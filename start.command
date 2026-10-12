#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .local/computer-use-helper ]; then
  sh scripts/build-native.sh
fi
exec python3 -m localcu
