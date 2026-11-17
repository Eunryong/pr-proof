from collections import Counter

import pytest

from app.models import proposal
from app.models.finding import Finding


def _patch(added: list[str], removed: int = 0) -> str:
    return "--- a/a.py\n+++ b/a.py\n" + "-x\n" * removed + "".join(f"+{a}\n" for a in added)


def test_branch_name() -> None:
    assert proposal.branch_name(12, "lint") == "pipeline/pr-12-fix-lint"


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "github_pat_" + "a" * 40,
        "xoxb-1234567890-abc",
        "https://hooks.slack.com/services/T000/B000/XXXX",
        "AKIA" + "A" * 16,
        "-----BEGIN RSA PRIVATE KEY-----",
        "sk-ant-" + "a" * 24,
    ],
)
def test_secret_patterns_block_push(secret: str) -> None:
    reason = proposal.push_blocker(_patch([f"X = '{secret}'"]), ["a.py"])
    assert reason == "비밀값으로 보이는 내용이 있음 (토큰·키 형식)"
    assert secret not in reason


def test_only_added_lines_are_scanned() -> None:
    """원래 코드에 있던 것을 지우는 수정은 막지 않는다."""
    patch = "--- a/a.py\n+++ b/a.py\n-X = 'ghp_" + "a" * 36 + "'\n+X = None\n"
    assert proposal.push_blocker(patch, ["a.py"]) is None


def test_pipeline_literal_secret() -> None:
    patch = _patch(["URL = 'my-webhook-secret'"])
    assert "파이프라인 비밀값" in proposal.push_blocker(patch, ["a.py"], ["my-webhook-secret"])
    # 너무 짧은 값은 흔한 문자열과 겹쳐 쓰지 않는다
    assert proposal.push_blocker(_patch(["x = 'abc'"]), ["a.py"], ["abc"]) is None


def test_limits() -> None:
    many = [f"f{i}.py" for i in range(proposal.MAX_FILES + 1)]
    assert "상한(20개)" in proposal.push_blocker(_patch(["x = 1"]), many)
    big = _patch(["x = 1"] * proposal.MAX_CHANGED_LINES, removed=1)
    assert "상한(500줄)" in proposal.push_blocker(big, ["a.py"])
    assert proposal.push_blocker(_patch(["x = 1"] * 499, removed=1), ["a.py"]) is None
    assert proposal.push_blocker("", ["a.py"]) == "바뀐 내용이 없음"


def test_fixed_codes_and_body() -> None:
    def f(code: str) -> Finding:
        return Finding("a.py", 1, "error", "m", "lint", "x", code, True)

    codes = proposal.fixed_codes([f("F401"), f("F401"), f("I001")], [f("I001")])
    assert codes == Counter({"F401": 2})
    body = proposal.body(3, "lint", "a" * 40, codes, "pyproject.toml", "통과 (9개)", 1)
    assert body.startswith(proposal.PROPOSAL_MARKER)
    assert "`F401` 2건" in body and "ruff (pyproject.toml)" in body
    assert "`ruff format`은 PR이 바꾼 파일 1개의 전체에 적용했습니다." in body
    assert "PR이 건드리지 않은 줄은 바꾸지 않았습니다" not in body  # 포맷하면 사실이 아니다
    assert proposal.title(3, "lint", 0) == "[pipeline] #3 lint 포맷 정리"


def _body(codes: dict[str, int], formatted: int = 0) -> str:
    return proposal.body(1, "lint", "a" * 40, Counter(codes), "x", "통과 (9개)", formatted)


def test_body_says_untouched_lines_only_when_true() -> None:
    assert "PR이 건드리지 않은 줄은 바꾸지 않았습니다." in _body({"F401": 1})


def test_body_mentions_import_block_rewrite_for_i001() -> None:
    """I001은 블록 전체를 다시 정렬하므로 "건드리지 않은 줄은 그대로"라고 쓰면 사실과 다르다."""
    body = _body({"F401": 1, "I001": 1})
    assert "`I001`(import 정렬)는 import 블록 전체를 다시 정렬했습니다." in body
    assert "PR이 건드리지 않은 줄은 바꾸지 않았습니다" not in body
