#!/bin/zsh
set -eu
cd "${0:A:h}/.."
swift build -c release
APP="$PWD/build/Folio.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/Folio "$APP/Contents/MacOS/Folio"
cp Resources/Info.plist "$APP/Contents/Info.plist"
if [[ -f Resources/Folio.icns ]]; then cp Resources/Folio.icns "$APP/Contents/Resources/"; fi
codesign --force --sign - --identifier app.vira.folio --entitlements Resources/Folio.entitlements "$APP"
echo "Built $APP"
