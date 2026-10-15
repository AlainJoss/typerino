#!/bin/bash
set -eu
cd -- "$1"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
  /usr/bin/osascript -e 'display alert "Typerino needs Python 3" message "Install Python 3 from python.org, then open Typerino again." as critical'
  exit 1
fi
if ! python3 "$(dirname -- "$0")/launch.py"; then
  /usr/bin/osascript -e 'display alert "Typerino could not start" message "Check .typerino.log in the project folder for details. If you started it in Terminal earlier, stop that copy and try again." as critical'
  exit 1
fi
