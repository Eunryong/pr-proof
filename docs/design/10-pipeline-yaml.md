## 10. `.pipeline.yaml` 스키마

대상 저장소의 base 브랜치에서 읽는다. 파일이 없으면 모두 기본값이다. 읽지 못하면 기본값으로 검사하고 이유를 코멘트의 "설정:"에 적는다. 모르는 키는 무시한다.

```yaml
rules:
  docs: [README.md, CONTRIBUTING.md]   # 규칙 문서 경로 (루트 기준). 다른 문서에 규칙을 적는 팀만 고친다
  disabled: []                         # 적용하지 않을 규칙 id. 코멘트의 "읽은 규칙"에 보이는 id를 적는다
                                       # 예: [R-readme-3fa9c2d1, PLR2004]. `ruff`는 ruff 설정의 규칙 묶음 전체
  from: pr                             # pr | base. PR이 규칙 문서를 고쳤을 때 읽는 쪽 (4.3 "규칙 문서의 원천").
                                       # pr: 믿는 작성자(`access.trusted`)의 PR은 그 PR의 문서로 읽은 규칙으로 검사한다.
                                       # base: 늘 base의 문서로 검사한다. 모르는 값은 pr로 읽고 "설정:"에 알린다

fix:
  require_approval: false              # 기본: 검사 뒤 바로 수정안 PR까지. true면 /fix를 기다린다 (interrupt)
  max_retries: 3                       # 모델 수정의 회차 상한. 1–5. 5를 넘으면 5로 줄이고 "설정:"에 알린다

tests:
  runner: pytest

sandbox:
  memory_gb: 1                         # 테스트 컨테이너 메모리 한도. 최소 0.5, 파이프라인 쪽 상한을 넘지 못한다

model:
  rules: sonnet                        # 규칙 읽기. claude --model에 넘긴다. 영문·숫자로 시작
  checker: sonnet                      # 검사기 작성. 3회 모두 이 모델이고 더 높은 모델로 올리지 않는다 (11.2 D7)
  decide: sonnet                       # 수정 판단 (4.6). 남은 위반을 묶어 한 번에 묻는다
  judge: sonnet                        # 모델의 직접 판단 (4.5). 검사기를 만들지 못한 규칙 (4단계)
  light: sonnet                        # 수정 작성의 가벼운 모델. heavy와 같으면 올리지 않는다
  heavy: sonnet                        # 수정 작성에서 실패가 이어지면 한 번 올리는 모델
  calls:                               # 모델 호출 수 상한 (9.2). 넉넉한 초깃값이고 측정 뒤 조정한다
    pr_base: 20                        # PR 상한 = pr_base + 대상 위반 수 × per_violation, 최대 pr_max
    per_violation: 4
    pr_max: 150
    ruleset_base: 10                   # 규칙 묶음 상한 = ruleset_base + 규칙 수 × per_rule, 최대 ruleset_max
    per_rule: 4
    ruleset_max: 100
    repo_daily: 500                    # 이 저장소의 하루 상한

pr_description:                        # PR 설명 확인 (4.5 끝): 제목·본문·커밋 메시지와 diff를 모델이 견주어
  enabled: true                        # 어긋난 곳을 결과 코멘트에 알린다. false면 PR 글을 읽지도 모델을 부르지도 않는다

linked_issues:                         # 이슈 확인 (4.5 끝): PR이 닫겠다고 한 이슈가 요구한 것과 diff를 모델이 견주어
  enabled: true                        # 빠진 것을 결과 코멘트에 알린다. false면 이슈를 읽지도 모델을 부르지도 않는다

access:                                # 외부 작성자 PR 가드레일 (8.6). PR이 아니라 base에서만 읽는다
  trusted: [OWNER, MEMBER, COLLABORATOR]  # 이 author_association의 PR은 바로 검사한다. 그 밖은 승인을 기다린다
  bots: []                             # 신뢰할 봇 계정의 로그인 이름. 예: ["dependabot[bot]"]. 봇은 승인할 수 없다

rag:
  top_k: 20                            # 검색이 가져오는 후보 수
  min_similarity: 0.91                 # 이 유사도 미만의 기록은 넣지 않는다. recall@k와 관련 없는 짝의 분포로 정했다 (6.7, 5단계)
  max_records: 3                       # 한 호출의 프롬프트에 넣는 기록 수 상한. top_k와 따로 둔다. 0–10
  enabled: auto                        # 과거 기록 검색. auto(기본) / on / off (6.7, 저장소 주인이 2026-11-20에 정함)
                                       # auto: 이 저장소의 채택·거절 기록 20건부터 켜고, 검색이 붙은 수정 시도가
                                       # 30건 이상 쌓였을 때 붙은 쪽 채택률이 안 붙은 쪽(10건 이상)보다 낮으면
                                       # 다시 끈다(다시 켜려면 on). 20, 30, 10은 초깃값. on·off는 조건을 보지 않는다
                                       # 옛 true/false는 on/off로 읽는다. 그 밖의 값은 auto로 읽고 "설정:"에 알린다
                                       # 기록 저장은 이 값과 관계없이 한다
```

운영 알림(macOS 알림, 13절 6단계)은 저장소 설정에도 파이프라인 쪽 설정에도 키가 없다. 대상 저장소가 끄거나 켜지 못한다. Slack 알림과 비밀값 파일 `slack_webhook_url`은 2026-11-20에 저장소 주인이 지웠다(11.1). 옛 `notify.slack.ops`도 같은 날 지웠다. 옛 파일에 남은 키는 모르는 키로 무시하고, 남은 `slack_webhook_url` 파일은 읽지 않는다.

**v1 스키마에서 바뀐 키**

| v1 | v2 | 처리 |
| --- | --- | --- |
| `skills.enabled: [lint, static, structure]` | 없음 | 스킬이 없어진다(3단계). 특정 규칙을 끄려면 `rules.disabled`. 옛 파일은 읽기만 한다: `lint`가 있으면 "설정:"에 없어졌다고 알리고, `lint`가 없으면(v1에서 ruff를 끈 저장소) 도구 설정의 규칙 묶음(`ruff`)을 `rules.disabled`에 더하고 새 키를 알린다 |
| `skills.fix.*` | `fix.*` | 구현 2단계 동안 옛 키도 읽고, 코멘트의 "설정:"에 새 키를 알린다 |
| `skills.static.min_repeats` | 없음 | 내장 정적변수 검사가 없어진다(2단계). 있으면 무시하고 "설정:"에 알린다 |
| `skills.structure.*` | 없음 | 구조 규칙은 문서에서 읽는다 |
| `model.backend` | 없음 | claude CLI만 쓴다. 읽은 적이 없는 키였다 |
| `model.budget_per_pr_usd` | `model.calls.*` | 달러 예산을 호출 수 상한으로 바꿨다 (9.2). 구현 2단계까지 옛 키가 있으면 무시하고 코멘트의 "설정:"에 알린다 |
| `extensions.*` | 없음 | v1 F13을 범위에서 뺐다 |
| `notify.slack.team` | 없음 | Slack 알림을 지웠다 (저장소 주인, 2026-11-20) |
| `fix.require_covered` | 없음 | 2026-11-21에 저장소 주인이 없앴다(11.1). 테스트가 실행하지 않는 줄도 고치고 수정안 PR에 그 사실을 적는다. `fix`나 옛 `skills.fix` 아래에 있으면 값과 관계없이 무시하고 "설정:"에 알린다 |
| `notify.slack.ops` | 없음 | Slack 알림을 지웠다. 남은 macOS 알림은 대상 저장소가 끄거나 켜지 못한다 (저장소 주인, 2026-11-20). 있으면 모르는 키로 무시한다 |

**파이프라인 쪽 설정 (`~/.config/pipeline/config.yaml`, 대상 저장소 밖)**

```yaml
repos: [owner/name]                    # 처리할 저장소 허용 목록
sync:
  interval_minutes: 5
  env_error_backoff_minutes: 30
  heartbeat_timeout_minutes: 20
sandbox:
  max_memory_gb: 4                     # 저장소가 올릴 수 있는 메모리 상한
model:
  default: opus                        # 없으면(기본) 쓰지 않는다. 이 컴퓨터가 처리하는 모든 저장소의 모든 단계에 쓸 모델.
                                       # 저장소가 단계별로 적은 값이 앞선다 (아래). `pr-proof model`과 configure가 적는다
  calls:                               # 저장소가 정할 수 있는 각 값의 최댓값
    max_pr_max: 500
    max_ruleset_max: 300
    max_repo_daily: 2000
rag:
  embed_model: intfloat/multilingual-e5-small   # 로컬 임베딩 모델. app/worker/store/rag_pure.py의 EMBED_MODELS에 판과 해시를 고정한 것만
```

**모델이 정해지는 순서 (2026-11-23, pr-proof#79).** 단계(`rules`, `checker`, `decide`, `judge`, `light`, `heavy`)마다 저장소의 `.pipeline.yaml`에 그 단계를 적었으면 그 값, 적지 않았으면 파이프라인 쪽 `model.default`, 그것도 없으면 위 스키마의 기본값이다. 적었는지는 저장소가 그 키를 썼는지로 가린다(기본값과 같은 값을 적어도 적은 것이다. pydantic의 `model_fields_set`, `app/worker/config/repo_config_pure.with_default_model`). `.pipeline.yaml`을 읽지 못해 기본값으로 읽은 저장소는 아무 단계도 적지 않은 것으로 본다. 파이프라인 쪽 값을 정하면 수정 작성의 `light`와 `heavy`도 그 모델 하나가 된다. 두 단계가 같은 모델이면 무거운 단계로 올리지 않고 회차를 모두 가벼운 단계로 센다(올려도 모델이 같아 호출 수만 셋으로 센다. `app/worker/fix/model_fix.py`). 저장한 판단은 키에 모델이 들어 있어(9.1) 모델이 바뀐 단계는 다음 검사에서 다시 묻는다. 이름은 꼴(`claude --model`이 받는 이름, 영문·숫자로 시작)만 확인하고 그 모델이 있는지는 부르기 전에는 보지 않는다. 없는 모델이면 그 단계의 호출이 실패로 끝나고 결과 코멘트에 그 단계가 실패했다고 나온다(규칙 읽기면 "규칙: 검사하지 못함"). 서버는 `config.yaml`을 시작할 때만 읽는다.
