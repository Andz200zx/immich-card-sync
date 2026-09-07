#!/bin/zsh
set -eu
package_dir="${0:A:h}"
for python_candidate in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [[ -x "$python_candidate" ]]; then
    exec "$python_candidate" "$package_dir/scripts/install.py" "$@"
  fi
done
if command -v python3 >/dev/null 2>&1; then
  exec python3 "$package_dir/scripts/install.py" "$@"
fi
print 'Install Python 3.10 or later from python.org or Homebrew, then run this installer again.'
exit 1
