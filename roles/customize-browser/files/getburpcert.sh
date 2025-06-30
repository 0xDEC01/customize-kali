#!/bin/bash

# Find the latest Burp JAR in the packaged directory
burp_jar=$(find /usr/share/burpsuite -maxdepth 1 -type f -name '*.jar' 2>/dev/null | tail -n1)
if [[ ! -f "$burp_jar" ]]; then
  echo "ERROR: Burp Suite JAR not found in /usr/share/burpsuite" >&2
  exit 1
fi

# Ensure system Java is available
if ! command -v java &>/dev/null; then
  echo "ERROR: Java not installed. Install default-jre first." >&2
  exit 1
fi

# Launch Burp headless for up to 45s, auto-accept the license prompt
timeout 45 java \
  -Djava.awt.headless=true \
  -jar "$burp_jar" <<< y &

# Give Burp time to start and expose its cert endpoint
sleep 30

# Download the CA certificate
curl -fsSL http://localhost:8080/cert -o /tmp/cacert.der

