#!/bin/zsh
set -eu
cd "${0:A:h}"
APP_NAME="模型核对"
BUILD_ROOT="$PWD/build"
APP_PATH="$HOME/Applications/$APP_NAME.app"
if [[ -e "$APP_PATH" ]]; then
  BUNDLE_ID=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP_PATH/Contents/Info.plist")
  [[ "$BUNDLE_ID" == "local.codex.model-observatory" ]] || { print -u2 -- "目标应用不是本项目生成，拒绝覆盖：$APP_PATH"; exit 1; }
fi
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources" "$BUILD_ROOT/module-cache"
/usr/bin/xcrun swiftc -swift-version 5 -O -module-cache-path "$BUILD_ROOT/module-cache" -framework AppKit -framework WebKit -framework UniformTypeIdentifiers "$PWD/main.swift" -o "$APP_PATH/Contents/MacOS/ModelObservatory"
/usr/bin/python3 - "$APP_PATH" "$PWD/../.." <<'PY'
import plistlib,sys
from pathlib import Path
bundle=Path(sys.argv[1])
info={'CFBundleName':'模型核对','CFBundleDisplayName':'模型核对','CFBundleExecutable':'ModelObservatory',
      'CFBundleIdentifier':'local.codex.model-observatory','CFBundlePackageType':'APPL',
      'CFBundleShortVersionString':'0.1','CFBundleVersion':'1','LSMinimumSystemVersion':'12.0',
      'NSHighResolutionCapable':True,'NSPrincipalClass':'NSApplication',
      'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},
      'NSHumanReadableCopyright':'Personal local monitor shell'}
(bundle/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
(bundle/'Contents/Resources/project-path.txt').write_text(str(Path(sys.argv[2]).resolve())+'\n',encoding='utf-8')
PY
/usr/bin/xattr -cr "$APP_PATH"
/usr/bin/codesign --force --sign - "$APP_PATH"
/usr/bin/codesign --verify --strict "$APP_PATH"
print -r -- "$APP_PATH"
