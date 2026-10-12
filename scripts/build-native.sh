#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
mkdir -p .local/module-cache .local/swift-cache .local/swift-config .local/swift-security
export CLANG_MODULE_CACHE_PATH="$PWD/.local/module-cache"
export SWIFTPM_MODULECACHE_OVERRIDE="$PWD/.local/module-cache"
swift build --package-path native -c release \
  --scratch-path "$PWD/.local/swift-build" \
  --cache-path "$PWD/.local/swift-cache" \
  --config-path "$PWD/.local/swift-config" \
  --security-path "$PWD/.local/swift-security" \
  "$@"
cp .local/swift-build/release/computer-use-helper .local/computer-use-helper
printf 'Native helper ready: .local/computer-use-helper\n'
