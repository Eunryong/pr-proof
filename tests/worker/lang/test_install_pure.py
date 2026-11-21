import pytest

from worker.lang.install_pure import UNKNOWN_FAILURE_LIMIT, install_failure_outcome


@pytest.mark.parametrize(
    ("kind", "failures", "outcome"),
    [
        ("resolution", 1, "unrunnable"),  # 1회에 PR 문제
        ("source_only", 1, "unrunnable"),
        ("disallowed", 1, "unrunnable"),
        ("network", 1, "environment"),
        ("network", 5, "environment"),  # 네트워크는 몇 번이든 환경 오류
        ("unknown", 1, "environment"),
        ("unknown", UNKNOWN_FAILURE_LIMIT, "unrunnable"),
        ("unknown", UNKNOWN_FAILURE_LIMIT + 1, "unrunnable"),
    ],
)
def test_install_failure_outcome(kind, failures: int, outcome: str) -> None:
    assert install_failure_outcome(kind, failures) == outcome
