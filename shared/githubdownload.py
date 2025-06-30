#!/usr/bin/env python3


# Description: Download GitHub release assets with single-call efficiency and controlled extraction.
# Original Author: Ippsec
# Version: 2.0

# Enhancements over v1.0:
#  • Single `/releases/latest` call for all patterns (helps avoid GitHub API rate limits)
#  • Multi-pattern support via argparse (`patterns`, `-n` overrides)
#  • Optional `-t/--token` + HTTPAdapter+Retry for token auth and transient-error retries
#  • Archive handling: extract in temp, then rename top-level folder per `-n` (like `tar -C`)

# Usage:
#  githubdownload.py <repo> <pattern1> [<pattern2> ...] [-n name1 name2 ...] [-o OUTPUT_DIR] [-t GITHUB_TOKEN]

# Arguments:
#  <repo>           GitHub repository in the form owner/repo (e.g., threathunters-io/laurel)
#  <pattern>        Regex pattern(s) to match release asset filenames
#  -n, --names      Optional custom names for the downloaded files or extracted folders
#  -o, --output     Destination directory (default: current directory)
#  -t, --token      Optional GitHub token to avoid rate limiting

# Examples:

# Download a single binary, renaming it to `chisel_linux`
#   python3 githubdownload.py jpillora/chisel 'linux_amd64.gz' -n chisel_linux

# Download and extract latest Chainsaw zip, rename folder to /opt/chainsaw
#   python3 githubdownload.py WithSecureLabs/chainsaw 'chainsaw.*linux.*\.zip' -o /opt -n chainsaw

# Download both linpeas and winpeas from same repo into /opt/peas/
#   python3 githubdownload.py carlospolop/PEASS-ng 'linpeas.sh' 'winPEASx64.exe' -o /opt/peas -n linpeas.sh winpeas.exe

# Use GitHub token to avoid rate limits when downloading many assets
#   python3 githubdownload.py some/repo 'asset1' 'asset2' -t $GITHUB_TOKEN


import argparse
import io
import gzip
import re
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
import shutil

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ARCHIVE_EXTS = (".zip", ".tar.gz", ".tgz")


def create_session(token=None):
    session = requests.Session()
    if token:
        session.headers.update({"Authorization": f"token {token}"})
    retry = Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


def get_latest_release(repo, session):
    r = session.get(f"https://api.github.com/repos/{repo}/releases/latest")
    r.raise_for_status()
    return r.json()


def find_assets(metadata, patterns):
    assets = metadata.get("assets", [])
    out = []
    for pat in patterns:
        rx = re.compile(pat)
        for a in assets:
            if rx.search(a["name"]):
                out.append(a)
                break
        else:
            raise ValueError(f"No asset matching /{pat}/")
    return out


def download_to_buffer(url, session):
    r = session.get(url, stream=True)
    r.raise_for_status()
    buf = io.BytesIO()
    for chunk in r.iter_content(8192):
        buf.write(chunk)
    buf.seek(0)
    return buf


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def extract_zip(buffer, dest: Path):
    with zipfile.ZipFile(buffer) as zf:
        zf.extractall(path=dest)


def extract_tar(buffer, dest: Path):
    with tarfile.open(fileobj=buffer, mode="r:*") as tar:
        tar.extractall(path=dest, filter="data")


def extract_gz_file(buffer, dest: Path, name: str):
    with gzip.open(buffer) as gz, open(dest / name, "wb") as f:
        f.write(gz.read())


def save_binary(buffer, path: Path):
    with open(path, "wb") as f:
        f.write(buffer.read())


def rename_extracted_folder(tmpdir: Path, outdir: Path, override: str = None):
    entries = list(tmpdir.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        src = entries[0]
        dest_name = override or src.name
        dest = outdir / dest_name
        if dest.exists():
            shutil.rmtree(dest)
        src.replace(dest)
        return dest
    folder_name = override or tmpdir.name
    dest = outdir / folder_name
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    for entry in entries:
        entry.replace(dest / entry.name)
    return dest


def main():
    parser = argparse.ArgumentParser(description="Download GitHub release assets")
    parser.add_argument("repo", help="owner/repo")
    parser.add_argument("patterns", nargs="+", help="Regex patterns for assets")
    parser.add_argument("-o", "--output", default=".", help="Parent output directory")
    parser.add_argument("-n", "--names", nargs="*", help="Override folder/file names per asset")
    parser.add_argument("-t", "--token", help="GitHub token (optional)")
    args = parser.parse_args()

    session = create_session(args.token)
    metadata = get_latest_release(args.repo, session)
    assets = find_assets(metadata, args.patterns)
    outdir = Path(args.output)
    ensure_dir(outdir)

    for idx, asset in enumerate(assets):
        buf = download_to_buffer(asset["browser_download_url"], session)
        orig = asset["name"]
        override = args.names[idx] if args.names and idx < len(args.names) else None
        suffix = orig.lower()

        if suffix.endswith(ARCHIVE_EXTS):
            with tempfile.TemporaryDirectory(dir=outdir) as td:
                tmpdir = Path(td)
                if suffix.endswith(".zip"):
                    extract_zip(buf, tmpdir)
                else:
                    extract_tar(buf, tmpdir)
                final = rename_extracted_folder(tmpdir, outdir, override)

        elif suffix.endswith(".gz"):
            # Safe .gz: detect embedded tar
            buf.seek(0)
            is_tar = False
            try:
                gz_fh = gzip.GzipFile(fileobj=buf)
                with tarfile.open(fileobj=gz_fh, mode="r:*") as tar:
                    is_tar = True
                    with tempfile.TemporaryDirectory(dir=outdir) as td:
                        tmpdir = Path(td)
                        tar.extractall(path=tmpdir, filter="tar")
                        final = rename_extracted_folder(tmpdir, outdir, override)
            except Exception:
                pass

            if not is_tar:
                buf.seek(0)
                name = override or orig[:-3]
                dest = outdir / name
                extract_gz_file(buf, outdir, name)

        else:
            name = override or orig
            dest = outdir / name
            save_binary(buf, dest)


if __name__ == "__main__":
    main()

