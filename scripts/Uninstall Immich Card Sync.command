#!/bin/zsh
set -eu
/bin/launchctl bootout "gui/$(id -u)/io.github.andz200zx.immich-card-sync" 2>/dev/null || true
/bin/rm -f "$HOME/Library/LaunchAgents/io.github.andz200zx.immich-card-sync.plist"
print 'Login startup is disabled. Quit the app from its menu if it was opened manually.'
print 'The app, settings, logs and Keychain credential are retained. No photos are changed.'
