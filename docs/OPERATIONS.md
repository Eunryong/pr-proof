# 운영: 띄워 두기와 알림

README에 있던 운영 설명을 옮겼다(2026-11-22). 글은 그대로다.

## 실행 스크립트

```
scripts/start.sh                 # api(웹훅 + 동기화 루프)와 worker. 어느 셸에서나 된다
```

`scripts/start.sh`는 이 체크아웃의 `.venv/bin`을 PATH 맨 앞에 두고 gh·claude·docker를 `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/bin`에서 찾아 `honcho start`를 실행한다. venv나 도구가 없으면 무엇이 없는지 적고 끝난다. 서버에는 `HOME`, `USER`, `TMPDIR`, `LANG`, `PIPELINE_CONFIG_DIR`과 정한 PATH만 넘기고, 비밀값은 읽지 않는다. venv를 켠 셸이면 `honcho start`도 같다.

워커는 시작할 때 DB 스키마를 맞추고(배포가 더한 테이블과 열), gh·claude 로그인, Docker, Redis를 확인하고(preflight) 하나라도 안 되면 macOS 알림을 띄우고 멈춘다. 스키마를 맞추지 못하면 로그만 남기고 알림 없이 멈춘다. 알림이 이 Mac에 뜨는지는 `PR_PROOF_REAL_NOTIFY=1 pytest tests/shared/test_notify_real.py`로 한 번 볼 수 있다. 샌드박스 이미지는 처음 빌드하고, 언어 런타임과 도구(Python, ruff, ast-grep)는 규칙이 쓸 때 고정한 버전과 해시로 받아 `~/.cache/pipeline/tools`에 둔다.

## launchd로 띄워 두기 (macOS)

서버가 죽거나 기계를 다시 켜도 다시 뜨게 하려면 사용자 LaunchAgent로 등록한다. 등록하기 전에 손으로 띄운 서버(`honcho start`, `scripts/start.sh`)를 먼저 멈춘다. 같은 큐에 워커가 둘이 되지 않게, launchd 밖에서 서버가 돌고 있으면 등록 스크립트가 거부한다.

```
scripts/install-launchd.sh                               # ~/Library/LaunchAgents/com.pr-proof.server.plist를 이 체크아웃의 절대 경로로 만들고 올린다
launchctl print gui/$(id -u)/com.pr-proof.server         # 상태 (state, pid, last exit code)
tail -f ~/Library/Logs/pr-proof/server.log               # 로그 (api와 worker의 stdout·stderr)
scripts/uninstall-launchd.sh                             # 멈추고 plist를 지운다. 로그는 남긴다
```

- 시스템 데몬이 아니라 사용자 에이전트다. gh·claude 로그인과 Docker Desktop이 로그인 세션에 있어서다. 그래서 "재부팅하면 다시 뜬다"는 "재부팅 뒤 사용자가 로그인하면 뜬다"이다.
- Docker는 띄우지 않는다. Docker Desktop을 "로그인할 때 시작"으로 설정해 둔다.
- 프로세스가 끝나면 launchd가 다시 띄운다. 다시 띄우는 간격은 60초(`ThrottleInterval`)다. Docker가 꺼졌거나 로그인이 만료되면 preflight가 워커를 멈추고 honcho가 api까지 내리므로, 고칠 때까지 60초마다 시작과 멈춤이 로그에 쌓인다. macOS 알림은 종류마다 `~/.cache/pipeline/alerts/`의 표시 파일로 세므로, 60초마다 다시 떠도 같은 오류의 알림은 30분에 한 번만 뜬다.
- 로그 파일은 돌려 쓰지 않는다. 커지면 손으로 비운다(`: > ~/Library/Logs/pr-proof/server.log`).
- 체크아웃을 옮기면 `scripts/uninstall-launchd.sh` 뒤 새 위치에서 `scripts/install-launchd.sh`를 다시 한다.

## 운영 알림

enssl rand -hex 32 > ~/.config/pipeline/webhook_secret && chmod 600 ~/.config/pipeline/webhook_secret
