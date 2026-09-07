#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
version="${1:-0.1.0}"
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then echo 'Use a version like 0.1.0' >&2; exit 1; fi
build_dir="build/$version"
package="$build_dir/Immich Card Sync"
app="$package/Immich Card Sync.app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources" "$package/scripts" dist
sdk="$(xcrun --sdk macosx --show-sdk-path)"
for arch in arm64 x86_64; do
  xcrun swiftc -sdk "$sdk" -target "$arch-apple-macosx13.0" -module-cache-path "$build_dir/module-cache" \
    -framework AppKit -framework Security src/CardSync.swift -o "$build_dir/CardSync-$arch"
done
lipo -create "$build_dir/CardSync-arm64" "$build_dir/CardSync-x86_64" -output "$app/Contents/MacOS/Immich Card Sync"
cp src/Info.plist "$app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $version" "$app/Contents/Info.plist"
cp src/ingest.py "$app/Contents/Resources/ingest.py"
cp scripts/install.py "$package/scripts/install.py"
cp scripts/*.command "$package/"
cp README.md LICENSE "$package/"
chmod +x "$package/"*.command
# Strip build-directory metadata before ad-hoc signing our own generated bundle.
xattr -cr "$app"
codesign --force --sign - "$app"
codesign --verify --deep --strict "$app"
ditto -c -k --keepParent "$package" "dist/Immich-Card-Sync-$version-universal.zip"
(cd dist && shasum -a 256 "Immich-Card-Sync-$version-universal.zip" > "Immich-Card-Sync-$version-SHA256SUMS.txt")
echo "Built dist/Immich-Card-Sync-$version-universal.zip"
