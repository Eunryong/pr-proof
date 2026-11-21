import pytest

from app.worker.fix import proposal_pure


def _patch(added: list[str], removed: int = 0) -> str:
    return "--- a/a.py\n+++ b/a.py\n" + "-x\n" * removed + "".join(f"+{a}\n" for a in added)


def test_branch_name() -> None:
    assert proposal_pure.branch_name(12, "lint") == "pipeline/pr-12-fix-lint"


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
    reason = proposal_pure.push_blocker(_patch([f"X = '{secret}'"]), ["a.py"])
    assert reason == "비밀값으로 보이는 내용이 있음 (토큰·키 형식)"
    assert secret not in reason


def test_only_added_lines_are_scanned() -> None:
    """원래 코드에 있던 것을 지우는 수정은 막지 않는다."""
    patch = "--- a/a.py\n+++ b/a.py\n-X = 'ghp_" + "a" * 36 + "'\n+X = None\n"
    assert proposal_pure.push_blocker(patch, ["a.py"]) is None


def test_pipeline_literal_secret() -> None:
    patch = _patch(["URL = 'my-webhook-secret'"])
    assert "파이프라인 비밀값" in proposal_pure.push_blocker(patch, ["a.py"], ["my-webhook-secret"])
    # 너무 짧은 값은 흔한 문자열과 겹쳐 쓰지 않는다
    assert proposal_pure.push_blocker(_patch(["x = 'abc'"]), ["a.py"], ["abc"]) is None


def test_limits() -> None:
    many = [f"f{i}.py" for i in range(proposal_pure.MAX_FILES + 1)]
    assert "상한(20개)" in proposal_pure.push_blocker(_patch(["x = 1"]), many)
    big = _patch(["x = 1"] * proposal_pure.MAX_CHANGED_LINES, removed=1)
    assert "상한(500줄)" in proposal_pure.push_blocker(big, ["a.py"])
    assert proposal_pure.push_blocker(_patch(["x = 1"] * 499, removed=1), ["a.py"]) is None
    assert proposal_pure.push_blocker("", ["a.py"]) == "바뀐 내용이 없음"


def test_find_secret_in_text_checks_the_whole_text() -> None:
    assert proposal_pure.find_secret_in_text("상수로 뺐다") is None
    assert proposal_pure.find_secret_in_text("키는 ghp_" + "a" * 36 + " 이다") == "토큰·키 형식"
    assert proposal_pure.find_secret_in_text("hook-secret-123 를 읽었다", ["hook-secret-123"]) == (
        "파이프라인 비밀값"
    )
    assert (
        proposal_pure.find_secret_in_text("short", ["short"]) is None
    )  # 너무 짧은 값은 보지 않는다
