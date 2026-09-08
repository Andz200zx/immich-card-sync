#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
for python in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
  if "$python" -c 'import sys; sys.exit(sys.version_info < (3,10))' 2>/dev/null; then
    "$python" scripts/install_worker.py --enable
    read -r -p 'Press Return to close. '
    exit 0
  fi
done
echo 'Install Python 3.10 or newer, RawTherapee and ExifTool before installing the worker.'
read -r -p 'Press Return to close. '
exit 1
