"""실제 claude CLI로 확인한다: PR 설명 확인이 PR 글과 diff를 견주어 어긋난 곳만 답한다 (4.5 끝).

작은 저장소 하나에서 네 경우를 본다. 모두 같은 base에서 갈라진 PR 커밋 하나다.

- (가) 설명과 변경이 맞음: 파서가 빈 줄을 건너뛰게 하고 그 테스트를 더함 → 어긋난 곳 없음.
- (나) 설명에 없는 동작 변경이 섞임: (가)에 더해 기본 타임아웃을 30초에서 5초로 줄이고, 다른
  파일의 import 순서와 공백만 정리함 → 「PR과 관련 없는 수정」은 타임아웃 하나뿐이다(포맷만 바꾼
  파일은 답하지 않는다).
- (다) 설명한 변경 하나가 없음: 오류 메시지를 한국어로 바꿨다고 했지만 바꾸지 않음 → 「설명에는
  있는데 코드에 없음」.
- (라) 설명과 숫자가 다름: 재시도를 3회로 한다고 했는데 코드는 5회 → 「설명과 다름」.
- (마) 파이프라인 수정안의 커밋이 PR에 들어옴: 그 커밋의 동작 변경은 어긋난 곳이 아니다.

(가)는 두 번째 확인이 저장한 답을 쓰는지(호출 0회)도 본다. 모델을 실제로 부르므로
`PR_PROOF_REAL_AI=1`일 때만 돈다. 호출은 모두 5회다. `-s`로 돌리면 경우마다 토큰, 도구 호출 수,
결과 코멘트의 글을 찍는다.
"""

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker import pr_description_report_pure as shown
from app.worker import repo
from app.worker.ai import claude_cli
from app.worker.budget import CallBudget
from app.worker.repo_config_pure import CallSettings
from app.worker.rules import pr_description
from app.worker.rules.pr_check_pure import Commit, PrFacts
from app.worker.rules.pr_description_pure import PrDescription
from app.worker.state import Store

from ...redis_stand_ins import MemoryKeys

REAL_AI_ENV: Final = "PR_PROOF_REAL_AI"
MODEL: Final = "sonnet"

PARSER: Final = '''\
from errors import BAD_LINE


def parse(lines):
    """줄마다 key=value를 읽어 dict로 돌려준다."""
    out = {}
    for line in lines:
        if "=" not in line:
            raise ValueError(BAD_LINE)
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out
'''
PARSER_SKIPS_BLANK: Final = PARSER.replace(
    "    for line in lines:\n",
    "    for line in lines:\n        if not line.strip():\n            continue\n",
)
PARSER_TEST: Final = """\
from parser import parse


def test_blank_lines_are_skipped():
    assert parse(["a=1", "", "  ", "b=2"]) == {"a": "1", "b": "2"}
"""
ERRORS: Final = 'BAD_LINE = "invalid line: expected key=value"\n'
CONFIG: Final = "TIMEOUT = 30\nRETRIES = 2\n"
CLIENT: Final = '''\
from config import RETRIES, TIMEOUT


def fetch(url, get):
    """get(url, timeout=...)을 RETRIES번까지 부른다."""
    for attempt in range(RETRIES):
        try:
            return get(url, timeout=TIMEOUT)
        except OSError:
            if attempt == RETRIES - 1:
                raise
'''
UTILS: Final = "import sys\nimport os\n\ndef join(a,b):\n    return os.path.join(a,b)\n"
UTILS_FORMATTED: Final = (
    "import os\nimport sys\n\n\ndef join(a, b):\n    return os.path.join(a, b)\n"
)
BASE_FILES: Final = {
    "parser.py": PARSER, "errors.py": ERRORS, "config.py": CONFIG, "client.py": CLIENT,
    "utils.py": UTILS,
}  # fmt: skip
BLANK_TITLE: Final = "파서가 빈 줄을 건너뛰게 한다"
BLANK_BODY: Final = (
    "입력에 빈 줄이나 공백만 있는 줄이 있으면 파서가 ValueError로 멈췄다. 이제 그런 줄은 "
    "건너뛴다.\n\n테스트: tests/test_parser.py에 빈 줄 테스트를 더했다."
)

pytestmark = pytest.mark.skipif(
    os.environ.get(REAL_AI_ENV) != "1", reason=f"{REAL_AI_ENV}=1일 때만 실제 모델을 부른다"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


def _pr(
    tmp_path: Path, changes: dict[str, str], title: str, body: str
) -> tuple[Path, str, PrFacts]:
    """(worktree, PR의 diff, PR 사실). base는 BASE_FILES, PR 커밋 하나가 changes를 쓴다."""
    wt = tmp_path / "wt"
    (wt / "tests").mkdir(parents=True)
    _git(wt, "init", "-q", "-b", "main")
    repo.record_git_dir(wt)
    for name, text in BASE_FILES.items():
        (wt / name).write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "base")
    for name, text in changes.items():
        (wt / name).write_text(text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", title)
    sha = _git(wt, "rev-parse", "HEAD").strip()
    facts = PrFacts(title=title, body=body, branch="feature", commits=(Commit(sha, title),),
                    paths=tuple(changes))  # fmt: skip
    return wt, _git(wt, "diff", "HEAD~1", "HEAD"), facts


def _check(wt: Path, diff: str, facts: PrFacts, store: Store, case: str) -> PrDescription:
    budget = CallBudget(store, "o/r", 1, "a" * 40, CallSettings())
    tools = pr_description.Tools(store=store, budget=budget, ask=claude_cli.run_structured,
                                 model=MODEL)  # fmt: skip
    out = pr_description.check(facts, diff, wt, tools)
    u = out.usage
    print(f"\n[{case}] 호출 {u.calls}, 저장 {u.cached}, 도구 {u.tool_uses}, 입력 {u.input_tokens}, "
          f"출력 {u.output_tokens}, 버림 {u.dropped}, 모델 {u.models}")  # fmt: skip
    print("\n".join([shown.title(out) or "", *shown.expanded(out)]))
    return out


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "pipeline.db", MemoryKeys())


def test_matching_description_has_no_mismatch(tmp_path: Path, store: Store) -> None:
    """(가) 같은 입력의 두 번째 확인은 저장한 답을 쓴다."""
    changes = {"parser.py": PARSER_SKIPS_BLANK, "tests/test_parser.py": PARSER_TEST}
    wt, diff, facts = _pr(tmp_path, changes, BLANK_TITLE, BLANK_BODY)
    first = _check(wt, diff, facts, store, "가")
    assert first.status == "checked" and first.usage.calls == 1, first
    assert first.items == (), first
    again = _check(wt, diff, facts, store, "가 다시")
    assert again.usage.calls == 0 and again.usage.cached == 1 and again.items == ()


def test_unrelated_behaviour_change_is_found_but_formatting_is_not(
    tmp_path: Path, store: Store
) -> None:
    """(나)"""
    changes = {
        "parser.py": PARSER_SKIPS_BLANK, "tests/test_parser.py": PARSER_TEST,
        "config.py": CONFIG.replace("TIMEOUT = 30", "TIMEOUT = 5"), "utils.py": UTILS_FORMATTED,
    }  # fmt: skip
    wt, diff, facts = _pr(tmp_path, changes, BLANK_TITLE, BLANK_BODY)
    out = _check(wt, diff, facts, store, "나")
    assert out.status == "checked", out
    assert [(m.kind, m.file) for m in out.items] == [("unrelated", "config.py")], out
    (m,) = out.items
    assert (m.side, m.line, m.code) == ("new", 1, "TIMEOUT = 5")


def test_described_change_missing_from_the_code_is_found(tmp_path: Path, store: Store) -> None:
    """(다)"""
    title = "파서: 빈 줄을 건너뛰고 오류 메시지를 한국어로 바꾼다"
    body = (
        "- 빈 줄이나 공백만 있는 줄은 건너뛴다.\n"
        "- key=value가 아닌 줄의 오류 메시지를 한국어로 바꿨다.\n"
    )
    changes = {"parser.py": PARSER_SKIPS_BLANK, "tests/test_parser.py": PARSER_TEST}
    wt, diff, facts = _pr(tmp_path, changes, title, body)
    out = _check(wt, diff, facts, store, "다")
    assert out.status == "checked", out
    assert [m.kind for m in out.items] == ["missing"], out
    assert "오류 메시지" in out.items[0].claim


def test_number_that_differs_from_the_description_is_found(tmp_path: Path, store: Store) -> None:
    """(라)"""
    title = "재시도 횟수를 3회로 늘린다"
    body = "네트워크가 잠깐 끊기면 요청이 실패했다. fetch의 재시도를 2회에서 3회로 늘린다."
    changes = {"config.py": CONFIG.replace("RETRIES = 2", "RETRIES = 5")}
    wt, diff, facts = _pr(tmp_path, changes, title, body)
    out = _check(wt, diff, facts, store, "라")
    assert out.status == "checked", out
    assert [(m.kind, m.file, m.line) for m in out.items] == [("different", "config.py", 2)], out


def test_change_from_a_merged_pipeline_fix_is_not_unrelated(tmp_path: Path, store: Store) -> None:
    """(마) 파이프라인 수정안이 PR에 머지되어 들어온 커밋의 변경(client.py의 동작 변경)은 PR 글이
    설명할 대상이 아니다. 그 커밋은 PR의 커밋으로 세지 않고 모델에 따로 보인다."""
    changes = {"parser.py": PARSER_SKIPS_BLANK, "tests/test_parser.py": PARSER_TEST}
    wt, _, facts = _pr(tmp_path, changes, BLANK_TITLE, BLANK_BODY)
    human = facts.commits[0]
    caught = CLIENT.replace("except OSError:", "except (OSError, TimeoutError):")
    (wt / "client.py").write_text(caught)
    _git(wt, "commit", "-qam", "[pipeline] 규칙 R-1 수정")
    pipeline_sha = _git(wt, "rev-parse", "HEAD").strip()
    facts = PrFacts(title=facts.title, body=facts.body, branch="feature", paths=facts.paths,
                    commits=(Commit(pipeline_sha, "[pipeline] 규칙 R-1 수정", pipeline=True),
                             human))  # fmt: skip
    diff = _git(wt, "diff", "HEAD~2", "HEAD")
    budget = CallBudget(store, "o/r", 1, "a" * 40, CallSettings())
    tools = pr_description.Tools(store=store, budget=budget, ask=claude_cli.run_structured,
                                 model=MODEL)  # fmt: skip
    pipeline = pr_description.pipeline_diffs(wt, facts)
    assert len(pipeline) == 1 and "TimeoutError" in pipeline[0]
    out = pr_description.check(facts, diff, wt, tools, pipeline=pipeline)
    u = out.usage
    print(f"\n[마] 호출 {u.calls}, 도구 {u.tool_uses}, 입력 {u.input_tokens}, 출력 "
          f"{u.output_tokens}, 버림 {u.dropped}")  # fmt: skip
    print("\n".join([shown.title(out) or "", *shown.expanded(out)]))
    assert out.status == "checked" and out.items == (), out


def test_next_commit_keeps_the_same_mismatch_in_the_same_words(
    tmp_path: Path, store: Store
) -> None:
    """(바) (나)의 PR에 동작과 관계없는 파일(notes.txt)만 더한 커밋이 올라온다. diff가 달라져 다시
    견주지만 바로 앞 결과를 같이 받아, 그대로인 타임아웃 어긋남을 같은 글로 유지한다. 호출 2회."""
    changes = {
        "parser.py": PARSER_SKIPS_BLANK, "tests/test_parser.py": PARSER_TEST,
        "config.py": CONFIG.replace("TIMEOUT = 30", "TIMEOUT = 5"), "utils.py": UTILS_FORMATTED,
    }  # fmt: skip
    wt, diff, facts = _pr(tmp_path, changes, BLANK_TITLE, BLANK_BODY)

    def check(diff: str, facts: PrFacts, case: str) -> PrDescription:
        head = _git(wt, "rev-parse", "HEAD").strip()
        budget = CallBudget(store, "o/r", 1, head, CallSettings())
        tools = pr_description.Tools(store=store, budget=budget, ask=claude_cli.run_structured,
                                     model=MODEL)  # fmt: skip
        out = pr_description.check(facts, diff, wt, tools)
        u = out.usage
        print(f"\n[{case}] 호출 {u.calls}, 도구 {u.tool_uses}, 입력 {u.input_tokens}, 출력 "
              f"{u.output_tokens}, 지난 결과 {u.earlier}, 지난 어긋남 {u.earlier_items}, 유지 "
              f"{u.kept_earlier}")  # fmt: skip
        print("\n".join([shown.title(out) or "", *shown.expanded(out)]))
        return out

    first = check(diff, facts, "바 첫 커밋")
    assert [(m.kind, m.file) for m in first.items] == [("unrelated", "config.py")], first
    (wt / "notes.txt").write_text("작업 메모: 빈 줄 처리 확인함\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "작업 메모를 더한다")
    sha = _git(wt, "rev-parse", "HEAD").strip()
    facts = PrFacts(title=facts.title, body=facts.body, branch="feature",
                    paths=(*facts.paths, "notes.txt"),
                    commits=(Commit(sha, "작업 메모를 더한다"), *facts.commits))  # fmt: skip
    second = check(_git(wt, "diff", "HEAD~2", "HEAD"), facts, "바 다음 커밋")
    assert second.usage.calls == 1 and second.usage.earlier == 1, second
    assert second.usage.kept_earlier == 1 and second.items == first.items, second
