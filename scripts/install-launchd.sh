#!/bin/bash
# pr-proof 서버를 이 사용자의 LaunchAgent로 등록한다. 로그인하면 뜨고, 죽으면 launchd가 다시 띄운다.
set -euo pipefail

label="com.pr-proof.server"
root="$(cd "$(dirname "$0")/.." && pwd)"
agents="$HOME/Library/LaunchAgents"
logs="$HOME/Library/Logs/pr-proof"
plist="$agents/$label.plist"
domain="gui/$(id -u)"

case "$root$HOME" in
  *[\&\<\>\|\\]*)
    echo "install-launchd.sh: 경로에 & < > | \\ 가 있으면 plist에 넣지 않는다: $root, $HOME" >&2
    exit 1
    ;;
esac
if launchctl print "$domain/$label" >/dev/null 2>&1; then
  echo "install-launchd.sh: $label 이 이미 등록되어 있다. scripts/uninstall-launchd.sh 로 먼저 지운다" >&2
  exit 1
fi
if pgrep -f 'app\.worker\.main|app\.api:app' >/dev/null; then
  echo "install-launchd.sh: launchd 밖에서 pr-proof 서버가 돌고 있다. 같은 큐에 워커가 둘이 되지 않게 먼저 멈춘다" >&2
  exit 1
fi

mkdir -p "$agents" "$logs"
sed -e "s|__CHECKOUT__|$root|g" -e "s|__LOGS__|$logs|g" "$root/ops/launchd/$label.plist" >"$plist"
plutil -lint "$plist" >/dev/null
launchctl bootstrap "$domain" "$plist"
echo "등록했다: $plist"
echo "상태: launchctl print $domain/$label"
echo "로그: $logs/server.log"
