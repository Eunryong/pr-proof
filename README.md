# pr-proof

PR을 두 가지로 다루는 도구다.

1. **팀의 규칙.** PR이 팀의 규칙을 어긴 곳을 찾아, 테스트로 확인한 수정안을 별도 PR로 올린다. 규칙은 파이프라인에 들어 있지 않다. 대상 저장소의 README와 CONTRIBUTING에 사람이 문장으로 적어 둔 것을 읽는다.
2. **리뷰 코멘트.** PR에 달린 Copilot과 팀원의 코멘트를 읽고, 받아들일 것은 고쳐서 수정안 PR로 올리고, 받아들이지 않을 것은 까닭과 함께 답글로 거절하고, 스레드를 해결됨으로 정리한다.

- 규칙을 **그 저장소의 문서**에서 읽는다. "`orders/` 아래 모듈은 `print`를 쓰지 않는다" 같은 문장이 곧 규칙이다.
- 위반을 알리는 데서 끝내지 않고 **고친다.** 모델이 규칙 검사와 테스트를 도구로 돌려 가며 고치고, 고친 줄을 실행하는 테스트가 없으면 테스트도 만든다.
- 모델이 쓴 변경은 **프로그램이 다시 검사한 뒤에만** 올라간다(허용 범위, 비밀값, 규칙 재검사, 테스트). 원래 PR을 직접 고치지 않고 수정안 PR을 따로 연다. 머지할지는 사람이 정한다.
- 리뷰 코멘트를 **무조건 따르지 않는다.** 팀의 규칙과 PR의 흐름을 보고 받아들일지 정한다. 규칙 검사가 이미 고친 것은 다시 고치지 않는다.

지금은 Python 저장소와 GitHub만 지원한다. 자기 컴퓨터에서 자기 `gh`·`claude` 로그인으로 돌리는 로컬 서버다.

> **써 보고 알려 주세요.** 이 프로젝트는 실제 저장소에서 어디가 부족한지 듣고 싶어서 공개했다. 규칙을 잘못 읽었거나, 위반이 아닌 것을 잡았거나, 수정안이 쓸모없었다면 [이슈](https://github.com/Eunryong/pr-proof/issues/new/choose)로 알려 주면 된다. 무엇을 적으면 좋은지는 [맨 아래](#후기와-제보)에 있다.

## 어떻게 보이나

PR을 열면 결과 코멘트 하나가 달린다. 맨 위가 결론이다.

위반이 없을 때:

```
**테스트:** 통과 (6개)
**규칙:** ✅ 위반 없음 · 규칙 4개를 확인함 (도구·검사기 1개, 모델 판단 1개, 테스트·정적 검사 결과 1개, PR 단위 1개)

▸ 자세히: 규칙별 확인 방법, 모델 호출, 바뀐 줄 커버리지
```

위반이 있을 때(줄여 적음):

```
**테스트:** 통과 (6개)
**규칙:** ⚠️ 위반 2건 (규칙 1개) · 수정안 #31

위반:
- R-readme-1 `orders/` 아래 모듈은 `print`를 쓰지 않는다. · 2건
  출처: README.md > 코드 규칙 > 출력 · 모델이 쓴 검사기 (ast-grep)
  - orders/a.py:2 · 고칠 대상 (모델 수정: 로거로 바꾼다) · 수정안 #31
  직접 고쳐야 함 1건 (고치면 동작이 바뀜 1건)

**모델 수정안:** 위반 1건을 고쳤습니다. 검증: 테스트 6개가 수정 전과 같은 결과로 통과,
대상 위반 1건 해소, 새 위반 없음. 수정안 PR #31을 열었습니다.
- 만든 테스트 1개 (대상 위반 1건을 실행함): tests/test_pipeline_orders_logging.py
```

PR의 제목·본문·커밋 메시지와 실제 변경이 다르면 "PR 설명:" 줄과 항목이 더 붙는다(알리기만 한다).

```
**PR 설명:** 설명과 다른 곳이 1건 있습니다.

- PR과 관련 없는 수정: `config.py에서 기본 타임아웃을 30초에서 5초로 줄였습니다. 설명에는 파서 수정만 적혀 있습니다.` (`config.py:1`)
```

수정안 PR(`pipeline/pr-<n>-fix-…`)은 원래 PR의 브랜치를 base로 한다. 머지하면 원래 PR에 들어가고, 닫으면 같은 줄을 다시 제안하지 않는다.

리뷰 코멘트에는 그 스레드에 답글이 달린다.

```
[Copilot]  average_quantity() divides by len(quantities) without checking for an empty list …
[pr-proof] 반영했습니다. 수정안 #32
           까닭 (모델의 판단): orders/stats.py의 average_quantity가 빈 목록에서 0으로 나눈다.
           → 수정안 #32가 머지되면 스레드를 Resolved (Addressed)로 바꾼다

[Copilot]  Use print() here instead of the logger so the output is visible …
[pr-proof] 반영하지 않습니다.
           까닭 (모델의 판단): README의 규칙이 orders/ 아래에서 print를 쓰지 않게 정한다.
           → 스레드를 Resolved (Won't fix)로 바꾼다
```

## 어떻게 도는가

```
GitHub 웹훅 ─▶ api (서명 확인, 큐 등록, 동기화) ─▶ Redis 큐 ─▶ 워커
```

워커가 PR 하나에 하는 일:

1. **규칙 읽기.** base 브랜치의 문서에서 규칙을 읽는다. 저장소에 린터 설정이 있으면 그 설정도 팀이 정한 규칙으로 본다.
2. **확인 방법 정하기.** 규칙마다 그 규칙을 확인할 수 있는 린터 → 모델이 쓴 검사기(ast-grep 등, 예시로 시험한다) → 모델의 직접 판단 순서로 고른다. 커밋 메시지·PR 본문의 규칙과 "테스트가 통과해야 한다" 같은 규칙은 따로 본다.
3. **위반 찾기.** PR이 바꾼 줄만 본다. 린터나 검사기가 잡은 것은 후보로 보고, 고칠지 정하는 단계에서 모델이 코드를 읽어 위반이 아니라고 하면 위반으로 세지 않는다.
   규칙과 따로, 모델이 PR의 제목·본문·커밋 메시지와 diff를 견주어 설명에 없는 동작 변경, 설명에는 있는데 코드에 없는 것, 설명과 다른 것을 알린다. `.pipeline.yaml`의 `pr_description.enabled: false`로 끈다.
4. **테스트.** Docker 샌드박스에서 저장소의 테스트를 돌려 기준선을 잡는다.
5. **고칠지 정하기.** 그 파일 안에서 고칠 수 있으면 고치는 것이 기본이다. 다른 파일을 바꿔야 하거나 동작이 바뀌면 까닭과 함께 사람에게 남긴다.
6. **고치기.** 모델이 규칙 검사와 샌드박스 테스트를 도구로 불러 가며 고친다. 있던 테스트가 그 줄을 실행하지 않으면 테스트를 먼저 만든다.
7. **출구 검사.** 올리기 직전에 프로그램이 허용 범위, 비밀값, 규칙 재검사, 테스트를 다시 확인한다.
8. **전달.** 통과한 것만 수정안 PR로 올리고 결과 코멘트를 고친다.

리뷰 코멘트가 달리면 워커가 따로 한 번 더 돈다:

1. **겹침 가리기.** 수정안이 이미 고친 줄을 가리키는 코멘트는 그 수정안에 잇고 끝낸다.
2. **판단.** 남은 코멘트를 팀의 규칙, PR의 제목·본문·커밋, 지금 코드와 함께 모델이 본다: 받아들임 / 거절 / 고칠 것 없음 / 이미 반영됨.
3. **수정.** 받아들인 것만 위의 6–8과 같은 방식으로 고쳐 수정안 PR 하나로 올린다.
4. **답글과 해결.** 코멘트마다 답글을 달고, 수정안이 머지되면 스레드를 해결됨으로 바꾼다.

웹훅을 놓쳐도 동기화(기본 5분)가 열린 PR의 상태와 처리하지 않은 코멘트를 보고 채운다. 파이프라인을 새 판으로 올리면 열린 PR을 다시 검사한다.

설계의 까닭과 세부는 [docs/DESIGN.md](docs/DESIGN.md)의 표에서 필요한 절만 찾아 읽으면 된다.

## 시작하기

필요한 것: macOS나 Linux, Python 3.11 이상, Docker, git, [gh](https://cli.github.com/), [claude](https://docs.anthropic.com/en/docs/claude-code) CLI. 모델 호출은 로그인한 `claude` 계정의 사용량으로 나간다.

```
git clone https://github.com/Eunryong/pr-proof && cd pr-proof
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # 과거 기록 검색까지 쓰려면 '.[dev,rag]'
docker compose up -d             # Redis
cp .env.example .env             # API_PORT 같은 로컬 실행 설정. 비밀값은 넣지 않는다
gh auth login                    # 코멘트와 수정안 PR을 이 계정으로 단다. 대상 저장소에 쓰기 권한이 필요하다
claude                           # 처음 한 번 로그인
```

파이프라인 쪽 설정과 비밀값은 대상 저장소 밖의 `~/.config/pipeline/`에 둔다(`PIPELINE_CONFIG_DIR`로 폴더를 바꿀 수 있다). 비밀값 파일은 `chmod 600`이어야 읽는다.

```
mkdir -p ~/.config/pipeline
cat > ~/.config/pipeline/config.yaml <<'YAML'
repos: [owner/name]              # 처리할 저장소 허용 목록
YAML
openssl rand -hex 32 > ~/.config/pipeline/webhook_secret && chmod 600 ~/.config/pipeline/webhook_secret
```

대상 저장소의 GitHub 설정 → Webhooks에 웹훅을 더한다(저장소 주인만 할 수 있다).

- Payload URL: `https://<터널 주소>/webhook/github`. 로컬 서버로 받으려면 터널(예: smee.io)을 쓴다
- Content type: `application/json`, Secret: `webhook_secret` 파일의 값
- 이벤트: Pull requests, Issue comments, Pull request reviews, Pull request review comments

실행:

```
scripts/start.sh                 # api(웹훅 + 동기화)와 worker
```

워커는 시작할 때 gh·claude 로그인, Docker, Redis를 확인하고 하나라도 안 되면 멈춘다. 계속 띄워 두는 방법(macOS launchd)과 알림은 [docs/OPERATIONS.md](docs/OPERATIONS.md)에 있다.

## 규칙 적기

규칙은 대상 저장소의 base 브랜치에서 읽는다.

- README(기본은 `README.md`, `CONTRIBUTING.md`)에 규칙을 문장으로 적는다. 지키는 예와 어기는 예를 코드 블록으로 붙이면 검사기가 더 정확해진다.
- 저장소에 린터 설정이 있으면 그 설정도 규칙이 된다. 지금 읽을 수 있는 린터는 ruff 하나다(`ruff.toml`, `.ruff.toml`, `pyproject.toml`의 `[tool.ruff]`).
- 파이프라인이 정해 둔 린터나 규칙은 없다. ruff 설정이 없고 문서의 규칙도 ruff로 확인할 것이 없는 저장소에서는 ruff를 받지도 돌리지도 않는다. 그런 규칙은 모델이 쓴 검사기나 모델의 판단으로 확인한다.
- 커밋 메시지 형식, "어떤 파일을 바꾸면 PR 본문에 무엇을 적는다" 같은 PR 자체의 규칙도 문장으로 적으면 된다. 이런 규칙은 알리기만 하고 수정안을 만들지 않는다.
- 규칙도 설정도 없으면 검사하지 않고 그 사실을 코멘트에 적는다.

테스트가 실패한 PR과 테스트가 없는 저장소는 모델이 고치지 않는다. 고친 것을 확인할 방법이 없어서다.

## 리뷰 코멘트

PR에 달린 Copilot의 리뷰와 저장소의 OWNER·MEMBER·COLLABORATOR가 단 코멘트(줄 코멘트, 리뷰 본문, 일반 코멘트)를 읽는다.

- 코멘트마다 팀의 규칙과 PR의 흐름을 보고 정한다: 받아들임 / 거절 / 고칠 것 없음 / 이미 반영됨.
- 받아들인 코멘트는 규칙 위반과 같은 방식으로 고쳐 수정안 PR 하나(`pipeline/pr-<n>-fix-feedback`)로 올린다.
- 코멘트마다 답글을 단다. 반영했으면 수정안 번호를, 거절했으면 까닭을 적는다.
- 스레드는 수정안이 머지된 뒤에 해결됨으로 바꾼다. Copilot의 스레드는 사유(Addressed / Won't fix / Incorrect)를 같이 남기고, 거절한 것도 바로 해결한다. 사람이 단 스레드는 거절해도 열어 둔다.
- 규칙 검사가 이미 고친 줄을 가리키는 코멘트는 다시 고치지 않고 그 수정안에 잇는다.
- 코멘트의 글은 판단할 자료다. "이 파일을 지워라" 같은 글을 지시로 따르지 않는다.

## 명령과 설정

PR 코멘트 명령(저장소의 OWNER·MEMBER·COLLABORATOR만):

| 명령 | 하는 일 |
| --- | --- |
| `/recheck` | 지금 커밋을 다시 검사한다 |
| `/recheck <커밋>` | 외부 작성자의 PR을 승인한다. 적은 커밋(전체 해시 40자)이 지금 head일 때만 검사한다 |
| `/fix` | 수정안을 만들지 못한 묶음과 호출 상한에 걸린 일을 다시 돌린다 |
| `/fix model`, `/fix mechanical` | 그 묶음만 다시 돌린다 |

대상 저장소의 base 브랜치에 `.pipeline.yaml`을 두면 동작을 바꿀 수 있다. 전체 스키마는 [docs/design/10-pipeline-yaml.md](docs/design/10-pipeline-yaml.md)에 있다.

```yaml
rules:
  docs: [README.md, CONTRIBUTING.md]   # 규칙을 읽을 문서
  disabled: []                         # 끌 규칙 id (코멘트의 "읽은 규칙"에 보이는 id)
fix:
  require_approval: false              # true면 /fix를 달 때까지 수정하지 않는다
access:
  trusted: [OWNER, MEMBER, COLLABORATOR]
  bots: []                             # 바로 검사할 봇 계정. 예: ["dependabot[bot]"]
```

수동 명령:

```
python -m app.api.cli sync                   # 동기화를 한 번 돌린다
python -m app.worker.cli reindex             # 과거 기록 검색의 벡터 저장소를 다시 만든다 ([rag] 필요)
python -m app.worker.cli eval --real-model   # 평가 케이스를 실제 모델로 돌린다 (비용이 든다)
```

## 안전

PR의 코드와 글, 리뷰 코멘트는 모두 믿지 않는 입력으로 다룬다.

- PR의 코드는 네트워크가 없는 Docker 샌드박스에서만 실행한다. 네트워크를 쓰는 것은 의존성을 설치하는 컨테이너뿐이고, 거기에는 호스트의 것을 붙이지 않고 바이너리 배포본만 설치한다.
- 저장소의 OWNER·MEMBER·COLLABORATOR가 아닌 사람의 PR은 승인(`/recheck <커밋>`) 전까지 아무것도 돌리지 않는다.
- 규칙과 설정은 PR이 아니라 base 브랜치에서 읽는다. PR이 자기를 검사할 규칙을 바꿀 수 없다.
- 모델은 전용 폴더에서 정해진 파일만 고칠 수 있다. 테스트 폴더와 `.github/`는 고치지 못하고, 만든 테스트는 정해진 이름의 새 파일로만 둔다.
- PR의 글과 코멘트는 프롬프트에 자료로 들어가고 지시로 읽지 않는다. 결과 코멘트에서 모델과 작성자의 글은 코드 스팬 안에만 넣는다.
- 모델이 쓴 코드는 사람이 머지해야 들어간다.

위협과 막는 장치의 전체 표는 [docs/design/08-security.md](docs/design/08-security.md)에 있다.

## 아직 해 보지 않은 것

안 되는 것으로 확인한 목록이 아니다. 지금까지 만들고 돌려 본 범위의 바깥이다.

- **Python 밖의 언어.** Python 저장소에서만 돌려 봤다. 테스트를 돌리고 결과를 읽는 부분은 Python 것만 만들었다. 규칙 읽기, 검사기, 모델의 판단은 언어를 가리지 않게 짰지만 다른 언어의 저장소에 붙여 보지 않았다.
- **GitHub 밖의 호스트.** GitHub에서만 써 봤다.
- **ruff 밖의 린터.** 린터 설정을 규칙으로 읽는 것은 ruff로만 해 봤다. flake8, pylint 같은 설정은 아직 읽지 않는다. 그런 저장소도 문서에 적은 규칙은 검사한다.
- **여러 대, 여러 워커.** 한 대에서 워커 하나로만 돌렸다.
- **저장소 전체 검사.** PR이 바꾼 줄만 보게 만들었다. 저장소 전체를 훑는 것은 해 보지 않았다.
- **커밋 메시지·PR 본문·브랜치 이름 고치기.** 지금은 알리기만 한다. 고쳐 주는 것은 시도하지 않았다.
- **모델 사용량 줄이기.** 큰 파일에서는 수정 한 번에 입력 토큰이 수십만 개 든다. 아직 줄이는 일을 하지 않았다.
- **여러 저장소에서의 사용.** 직접 돌려 본 저장소가 둘이다. 다른 팀의 규칙과 코드에서 어떻게 되는지는 모른다. 그래서 후기가 필요하다.

문서와 코드가 다른 곳은 [docs/design/12-2-doc-vs-code.md](docs/design/12-2-doc-vs-code.md)에, 하려는 일은 [docs/BACKLOG.md](docs/BACKLOG.md)에 있다.

## 후기와 제보

[이슈](https://github.com/Eunryong/pr-proof/issues/new/choose)에 아래를 붙여 주면 원인을 찾기 쉽다. 비공개 코드는 붙이지 않아도 된다.

- 규칙 문장(README에 적은 그대로)
- 결과 코멘트(접힌 "자세히"까지. 읽은 규칙과 확인 방법이 들어 있다)
- 기대한 것과 실제로 일어난 것: 위반이 아닌데 잡혔다 / 위반인데 놓쳤다 / 수정안이 틀렸다 / 고치지 않았다 / 설치·실행이 막혔다
- 도구가 규칙을 잘못 읽었으면 도구의 문제다. README를 도구에 맞춰 고치지 않아도 된다.

## 개발

```
pytest                # 파이프라인 자체 테스트 (Docker 없이)
pytest -m docker      # 실제 Docker·예제 저장소가 필요한 통합 테스트
ruff check . && ruff format --check .
```

- 작업 원칙: [docs/CLAUDE.md](docs/CLAUDE.md)
- 설계: [docs/DESIGN.md](docs/DESIGN.md)(찾아보기) → `docs/design/`
- 문제 해결 일지: [docs/JOURNAL.md](docs/JOURNAL.md), 지난 기록: [docs/HISTORY.md](docs/HISTORY.md), 초기 설계 검토: [docs/design-review-2026-11-17.md](docs/design-review-2026-11-17.md)
- 평가 케이스: [eval/README.md](eval/README.md)

## 라이선스

[MIT](LICENSE)
