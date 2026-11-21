#!/bin/bash
# install-launchd.sh가 등록한 LaunchAgent를 멈추고 지운다. 로그는 남긴다.
set -euo pipefail

label="com.pr-proof.server"
plist="$HOME/Library/LaunchAgents/$label.plist"
domain="gui/$(id -u)"

if launchctl print "$domain/$label" >/dev/null 2>&1; then
  launchctl bootout "$domain/$label"
  echo "멈췄다: $domain/$label"
else
  echo "등록되어 있지 않다: $domain/$label"
fi
rm -f "$plist"
echo "지웠다: $plist"
