#!/usr/bin/env bash
# Generate and install the launchd plist for auto-start on macOS.
# Usage: ./scripts/install-daemon.sh

set -euo pipefail

HOME_DIR="$HOME"
BUN_PATH="$(which bun 2>/dev/null || echo "$HOME_DIR/.bun/bin/bun")"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_NAME="com.smolclaw.daemon"
PLIST_PATH="$HOME_DIR/Library/LaunchAgents/$PLIST_NAME.plist"
DATA_DIR="$HOME_DIR/.smolclaw"

mkdir -p "$DATA_DIR"
mkdir -p "$HOME_DIR/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>

    <key>ProgramArguments</key>
    <array>
        <string>$BUN_PATH</string>
        <string>run</string>
        <string>$PROJECT_DIR/src/index.ts</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>$DATA_DIR/daemon-stdout.log</string>

    <key>StandardErrorPath</key>
    <string>$DATA_DIR/daemon-stderr.log</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$(dirname "$BUN_PATH"):/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
EOF

echo "Plist written to: $PLIST_PATH"

# Load the daemon
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

echo "Daemon loaded. smolclaw will start automatically on login."
echo "  Stop:    launchctl unload $PLIST_PATH"
echo "  Start:   launchctl load $PLIST_PATH"
echo "  Logs:    tail -f $DATA_DIR/daemon-stdout.log"
