#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hardened GitHub release asset downloader.

- Handles: .zip, .tar.gz/.tgz/.tar.xz/.txz/.tar.bz2/.tbz2, plain .gz (single file), and raw files.
- Safe extraction (no path traversal), tar extract uses filter="data".
- Robust rename behavior for single-file vs single-dir vs multi-entry archives.
- --allow-missing lets you skip absent patterns without failing CI/Ansible.
- --chmod can set modes on final top-level files (e.g., 0755).

Usage:
  githubdownload.py <owner/repo> <pattern1> [<pattern2> ...]
                    [-o OUTDIR] [-n NAME1 NAME2 ...]
                    [--chmod MODE1 MODE2 ...]
                    [--allow-missing] [-t TOKEN]
"""

import argparse
import gzip
import io
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".tbz2")

def create_session(token: str | None = None) -> requests.Session:
    s = requests.Session()
    if token:
        s.headers.update({"Authorization": f"token {token}"})
    retry = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

def get_latest_release(repo: str, session: requests.Session) -> dict:
    r = session.get(f"https://api.github.com/repos/{repo}/releases/latest")
    r.raise_for_status()
    return r.json()

def find_assets(metadata: dict, patterns: list[str], allow_missing: bool) -> list[dict]:
    assets = metadata.get("assets", [])
    out: list[dict] = []
    for pat in patterns:
        rx = re.compile(pat)
        matched = next((a for a in assets if rx.search(a["name"])), None)
        if matched is None:
            if allow_missing:
                print(f"[SKIP] No asset matching /{pat}/ (allowed to miss)", file=sys.stderr)
                continue
            raise ValueError(f"No asset matching /{pat}/")
        out.append(matched)
    return out

def download_to_buffer(url: str, session: requests.Session) -> io.BytesIO:
    r = session.get(url, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(8192):
        buf.write(chunk)
    buf.seek(0)
    return buf

def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)

# -------- safe extraction helpers --------

def _is_within(base: Path, target: Path) -> bool:
    try:
        base = base.resolve(strict=False)
        target = target.resolve(strict=False)
    except Exception:
        return False
    return os.path.commonpath([base]) == os.path.commonpath([base, target])

def extract_zip_safe(buffer: io.BytesIO, dest: Path) -> None:
    with zipfile.ZipFile(buffer) as zf:
        for info in zf.infolist():
            # Normalize paths and prevent traversal
            member_path = dest / info.filename
            if not _is_within(dest, member_path):
                raise ValueError(f"Unsafe zip path: {info.filename}")
        zf.extractall(path=dest)

def extract_tar_safe(buffer: io.BytesIO, dest: Path) -> None:
    # r:* auto-detects compression (gz, bz2, xz)
    with tarfile.open(fileobj=buffer, mode="r:*") as tar:
        tar.extractall(path=dest, filter="data")  # safe; blocks symlink/hardlink extraction

def extract_gz_single_file(buffer: io.BytesIO, dest: Path, name: str) -> Path:
    with gzip.open(buffer) as gz:
        content = gz.read()
    out = dest / name
    with open(out, "wb") as f:
        f.write(content)
    return out

def save_binary(buffer: io.BytesIO, path: Path) -> Path:
    with open(path, "wb") as f:
        f.write(buffer.read())
    return path

# -------- rename/move semantics --------

def materialize(tmpdir: Path, outdir: Path, override_name: str | None) -> Path:
    """
    Moves extracted content from tmpdir into outdir.

    Rules:
      - single FILE  -> outdir/<override or file.name> (a file)
      - single DIR   -> outdir/<override or dir.name>  (a directory)
      - many entries -> outdir/<override or tmpdir.name>/... (a directory)
    """
    entries = [p for p in tmpdir.iterdir() if p.name != "__MACOSX"]
    if len(entries) == 1 and entries[0].is_file():
        final = outdir / (override_name or entries[0].name)
        final.parent.mkdir(parents=True, exist_ok=True)
        # If final equals the same file path, don't "replace" it with itself.
        if entries[0].resolve() != final.resolve():
            entries[0].replace(final)
        return final

    if len(entries) == 1 and entries[0].is_dir():
        src = entries[0]
        dest = outdir / (override_name or src.name)
        if dest.exists():
            shutil.rmtree(dest)
        src.replace(dest)
        return dest

    # multiple entries
    dest = outdir / (override_name or tmpdir.name)
    dest.mkdir(parents=True, exist_ok=True)
    for e in entries:
        target = dest / e.name
        if e.is_dir():
            if target.exists():
                shutil.rmtree(target)
            e.replace(target)
        else:
            e.replace(target)
    return dest

# -------- main --------

def main():
    parser = argparse.ArgumentParser(description="Download GitHub release assets")
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("patterns", nargs="+", help="Regex patterns for assets")
    parser.add_argument("-o", "--output", default=".", help="Parent output directory")
    parser.add_argument("-n", "--names", nargs="*", help="Override final names per asset (file OR folder)")
    parser.add_argument("--chmod", nargs="*", help="chmod modes per asset (e.g. 0755). Applied to top-level files.")
    parser.add_argument("--allow-missing", action="store_true", help="Do not fail if a pattern is absent")
    parser.add_argument("-t", "--token", help="GitHub token (optional)")
    args = parser.parse_args()

    session = create_session(args.token)
    meta = get_latest_release(args.repo, session)
    assets = find_assets(meta, args.patterns, allow_missing=args.allow_missing)
    outdir = Path(args.output)
    ensure_dir(outdir)

    for idx, asset in enumerate(assets):
        buf = download_to_buffer(asset["browser_download_url"], session)
        orig = asset["name"]
        override = args.names[idx] if args.names and idx < len(args.names) else None
        chmod_mode = None
        if args.chmod and idx < len(args.chmod):
            try:
                chmod_mode = int(args.chmod[idx], 8)
            except Exception:
                print(f"[WARN] Invalid chmod '{args.chmod[idx]}' ignored", file=sys.stderr)

        lower = orig.lower()

        if lower.endswith(ARCHIVE_SUFFIXES):
            with tempfile.TemporaryDirectory(dir=outdir) as td:
                tmpdir = Path(td)
                # .zip
                if lower.endswith(".zip"):
                    extract_zip_safe(buf, tmpdir)
                else:
                    # any .tar.*
                    buf.seek(0)
                    extract_tar_safe(buf, tmpdir)
                final = materialize(tmpdir, outdir, override)

        elif lower.endswith(".gz"):
            # Could be tar or single-file gzip
            is_tar = False
            buf.seek(0)
            try:
                # Try treating the *decompressed* stream as a tar
                gz_fh = gzip.GzipFile(fileobj=buf)
                with tarfile.open(fileobj=gz_fh, mode="r:*") as tar:
                    is_tar = True
                    with tempfile.TemporaryDirectory(dir=outdir) as td:
                        tmpdir = Path(td)
                        tar.extractall(path=tmpdir, filter="data")
                        final = materialize(tmpdir, outdir, override)
            except tarfile.TarError:
                pass

            if not is_tar:
                buf.seek(0)
                target_name = override or orig[:-3]
                final = extract_gz_single_file(buf, outdir, target_name)

        else:
            # Raw file (no archive)
            name = override or orig
            final = save_binary(buf, outdir / name)

        # chmod if requested and final is a file
        if chmod_mode is not None and final.is_file():
            os.chmod(final, chmod_mode)

    # If we skipped everything due to allow-missing, still exit 0
    print("[OK] Download(s) complete.")

if __name__ == "__main__":
    main()
