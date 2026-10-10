#!/bin/sh
# Re-run the WebKit spike (macOS 14+, Xcode command line tools). Throwaway harness, not product code.
# Results: events.jsonl (server side) + stdout (app side). See docs/design/SAFARI-FACTS.md.
set -e
cd "$(dirname "$0")"
WORK="${TMPDIR:-/tmp}/pp-webkit-probe"; mkdir -p "$WORK/Probe.app/Contents/MacOS"
cp servers.py "$WORK/"
cat > "$WORK/Probe.app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.profilepilot.wkprobe</string>
<key>CFBundleExecutable</key><string>probe</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsArbitraryLoads</key><true/></dict>
</dict></plist>
PLIST
swiftc -swift-version 5 -O -o "$WORK/Probe.app/Contents/MacOS/probe" probe.swift -framework WebKit -framework AppKit -framework Network
swiftc -swift-version 5 -o "$WORK/features" features.swift -framework WebKit
codesign -s - --force "$WORK/Probe.app"
LAN="$(ipconfig getifaddr en0 || echo 127.0.0.1)"
PY="${PYTHON:-python3}"   # needs the 'websockets' package (ProfilePilot's venv has it)
( cd "$WORK" && "$PY" servers.py ) & SERVERS=$!
trap 'kill $SERVERS' EXIT
until grep -q '"ready"' "$WORK/events.jsonl" 2>/dev/null; do sleep 0.2; done
P="$WORK/Probe.app/Contents/MacOS/probe"
"$P" persist1 && "$P" persist2 && "$P" main "$LAN" && "$P" phase2 "$LAN" && "$P" offscreen && "$P" offscreen noocclusion
"$WORK/features"
echo "Real Safari comparison: open -g -a Safari 'http://127.0.0.1:47801/fp?src=safari' while the servers run."
