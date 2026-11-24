import pytest

from app.worker.core import secret_pure
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


def _two_files(code_lines: int, test_lines: int) -> str:
    return (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n"
        + "+x = 1\n" * code_lines
        + "diff --git a/tests/test_pipeline_a.py b/tests/test_pipeline_a.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/tests/test_pipeline_a.py\n@@ -0,0 +1 @@\n"
        + "+assert True\n"
        * test_lines
    )


def test_generated_tests_do_not_count_toward_the_fix_size() -> None:
    # 출구 검사는 만든 테스트를 수정의 상한과 따로 센다. 올리기 직전의 확인도 같아야 통과한
    # 수정안이 여기서 버려지지 않는다
    test = "tests/test_pipeline_a.py"
    patch = _two_files(300, 300)
    assert "상한(500줄)" in proposal_pure.push_blocker(patch, ["a.py", test])
    assert proposal_pure.push_blocker(patch, ["a.py", test], test_files=[test]) is None
    assert proposal_pure.changed_line_count(patch) == 600
    assert proposal_pure.changed_line_count(patch, [test]) == 300
    # 코드 줄이 상한을 넘으면 테스트를 빼도 막는다
    assert "상한(500줄)" in proposal_pure.push_blocker(
        _two_files(501, 10), ["a.py", test], test_files=[test]
    )


def test_generated_test_files_do_not_count_toward_the_file_limit() -> None:
    code = [f"f{i}.py" for i in range(proposal_pure.MAX_FILES)]
    tests = [f"tests/test_pipeline_{i}.py" for i in range(3)]
    patch = _patch(["x = 1"])
    assert "상한(20개)" in proposal_pure.push_blocker(patch, code + tests)
    assert proposal_pure.push_blocker(patch, code + tests, test_files=tests) is None


def test_secrets_are_still_found_in_generated_tests() -> None:
    test = "tests/test_pipeline_a.py"
    patch = _two_files(1, 0) + "+TOKEN = 'ghp_" + "a" * 36 + "'\n"
    reason = proposal_pure.push_blocker(patch, ["a.py", test], test_files=[test])
    assert reason is not None and proposal_pure.SECRET_BLOCKED in reason


def test_find_secret_in_text_checks_the_whole_text() -> None:
    assert secret_pure.find_secret_in_text("상수로 뺐다") is None
    assert secret_pure.find_secret_in_text("키는 ghp_" + "a" * 36 + " 이다") == "토큰·키 형식"
    assert secret_pure.find_secret_in_text("hook-secret-123 를 읽었다", ["hook-secret-123"]) == (
        "파이프라인 비밀값"
    )
    assert secret_pure.find_secret_in_text("short", ["short"]) is None  # 너무 짧은 값은 보지 않는다
