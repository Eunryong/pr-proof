"""app/sandbox/ruff_runner.py 단위 테스트. docker 없이 run_bounded의 결과를 정해 둔다."""

import json
from pathlib import Path
from typing import Final

import pytest

from app.models.text_edit import TextEdit
from app.models.tools import RuffConfig
from app.sandbox import ruff_runner
from app.sandbox.checker_runner import Failed, Match, Ran, RawRun
from app.sandbox.docker_cli import SandboxError

CONFIG: Final = RuffConfig("ruff.toml", "line-length = 90\n", "b" * 40)


def _item(file: str, row: int, end_row: int | None = None, code: str | None = "F401") -> dict:
    return {
        "filename": f"/work/{file}",
        "location": {"row": row, "column": 1},
        "end_location": {"row": end_row or row, "column": 5},
        "code": code,
        "message": "msg",
    }


@pytest.fixture
def fake_run(monkeypatch, tmp_path: Path):
    calls: list[list[str]] = []
    seen_config: list[str] = []

    def install(result: RawRun):
        def run(argv, stdin, *, timeout):
            calls.append(argv)
            if "--config" in argv:  # 설정 사본은 임시 폴더에 있다가 실행이 끝나면 지워진다
                mount = next(a for a in argv if a.endswith(f":{ruff_runner.CONFIG_MOUNT}:ro"))
                seen_config.append((Path(mount.split(":")[0]) / CONFIG.name).read_text())
            return result

        monkeypatch.setattr(ruff_runner, "run_bounded", run)
        return calls, seen_config

    return install


def _ok(stdout: bytes = b"[]", code: int = 0) -> RawRun:
    return RawRun(code, stdout, "")


def test_check_mounts_the_base_config_outside_the_tree_and_paths_after_dashes(
    fake_run, tmp_path: Path
) -> None:
    calls, configs = fake_run(_ok())
    ruff_runner.check(tmp_path, ["-a.py"], tool_dir=tmp_path, config=CONFIG, codes=["F401", "T201"])
    (argv,) = calls
    assert argv[argv.index("--config") + 1] == "/config/ruff.toml" and configs == [CONFIG.text]
    assert argv[argv.index("--select") + 1] == "F401,T201"
    assert argv[-2:] == ["--", "-a.py"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert f"{tmp_path.resolve()}:/work:ro" in argv  # 검사할 폴더는 읽기 전용
    assert "--read-only" in argv and "--cap-drop" in argv


def test_check_without_config_is_isolated(fake_run, tmp_path: Path) -> None:
    calls, _ = fake_run(_ok())
    ruff_runner.check(tmp_path, ["a.py"], tool_dir=tmp_path, config=None)
    (argv,) = calls
    assert "--isolated" in argv and "--config" not in argv and "--select" not in argv


def test_no_files_runs_nothing(fake_run, tmp_path: Path) -> None:
    calls, _ = fake_run(_ok())
    assert ruff_runner.check(tmp_path, [], tool_dir=tmp_path, config=None) == Ran(())
    assert calls == []


@pytest.mark.parametrize("code", [0, 1])
def test_violations_are_read_relative_to_the_repo(fake_run, tmp_path: Path, code: int) -> None:
    out = json.dumps([_item("a.py", 3, 5), _item("b.py", 1, code=None)]).encode()
    fake_run(_ok(out, code))
    run = ruff_runner.check(tmp_path, ["a.py", "b.py"], tool_dir=tmp_path, config=None)
    assert run == Ran(
        (Match("a.py", 3, 5, "msg", "F401"), Match("b.py", 1, 1, "msg", "syntax-error"))
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (RawRun(2, b"", "bad config"), "ruff 실행 오류: bad config"),
        (RawRun(137, b"", ""), "메모리"),
        (RawRun(None, b"", "", timed_out=True), "초 안에 끝나지 않음"),
        (RawRun(0, b"{", ""), "ruff 출력을 읽지 못함"),
        (RawRun(1, json.dumps([_item("other.py", 1)]).encode(), ""), "넘기지 않은 파일"),
    ],
    ids=["config-error", "killed", "timeout", "broken-json", "outside"],
)
def test_unusable_results_fail_the_rule_not_the_job(fake_run, tmp_path: Path, raw, reason) -> None:
    fake_run(raw)
    run = ruff_runner.check(tmp_path, ["a.py"], tool_dir=tmp_path, config=None)
    assert isinstance(run, Failed) and reason in run.reason


def test_docker_failure_is_environment_error(fake_run, tmp_path: Path) -> None:
    fake_run(RawRun(125, b"", "daemon down"))
    with pytest.raises(SandboxError):
        ruff_runner.check(tmp_path, ["a.py"], tool_dir=tmp_path, config=None)


def test_only_safe_fixes_become_edits() -> None:
    edit = {"location": {"row": 1, "column": 1}, "end_location": {"row": 2, "column": 1},
            "content": ""}  # fmt: skip
    safe = {**_item("a.py", 1), "fix": {"applicability": "safe", "edits": [edit]}}
    unsafe = {**_item("a.py", 2), "fix": {"applicability": "unsafe", "edits": [edit]}}
    first, second = ruff_runner.parse_check(json.dumps([safe, unsafe]))
    assert first.edits == (TextEdit(1, 1, 2, 1, ""),) and second.edits == ()


def test_format_diff_gives_changed_original_lines() -> None:
    diff = (
        "--- /work/pkg/a.py\n+++ /work/pkg/a.py\n@@ -1,4 +1,4 @@\n import os\n-x=1\n+x = 1\n"
        " y = 2\n-print( 1 )\n+print(1)\n--- b.py\n+++ b.py\n@@ -3,2 +3,3 @@\n a\n+\n b\n"
    )
    assert ruff_runner.parse_format_diff(diff) == (
        Match("pkg/a.py", 2, 2, ruff_runner.FORMAT_MESSAGE, ruff_runner.FORMAT_CODE),
        Match("pkg/a.py", 4, 4, ruff_runner.FORMAT_MESSAGE, ruff_runner.FORMAT_CODE),
        Match("b.py", 3, 3, ruff_runner.FORMAT_MESSAGE, ruff_runner.FORMAT_CODE),
    )


@pytest.mark.parametrize(("code", "expected"), [(0, True), (1, False)])
def test_format_check(fake_run, tmp_path: Path, code: int, expected: bool) -> None:
    calls, _ = fake_run(_ok(b"", code))
    assert ruff_runner.format_check(tmp_path, tool_dir=tmp_path, config=None) is expected
    assert "--check" in calls[0] and calls[0][-2:] == ["--", "."]


def test_format_apply_is_the_only_writable_mount(fake_run, tmp_path: Path) -> None:
    calls, _ = fake_run(_ok())
    assert ruff_runner.format_apply(tmp_path, ["a.py"], tool_dir=tmp_path, config=None) is True
    assert f"{tmp_path.resolve()}:/work" in calls[0] and calls[0][-2:] == ["--", "a.py"]
    assert "--network" in calls[0] and "--check" not in calls[0]


def test_format_error_is_a_reason(fake_run, tmp_path: Path) -> None:
    fake_run(RawRun(2, b"", "boom"))
    result = ruff_runner.format_check(tmp_path, tool_dir=tmp_path, config=None)
    assert isinstance(result, str) and "boom" in result


def test_rule_exists_passes_only_rule_codes(fake_run, tmp_path: Path) -> None:
    calls, _ = fake_run(_ok(b"", 0))
    assert ruff_runner.rule_exists("--help", tool_dir=tmp_path) is False
    assert calls == []  # 코드 모양이 아니면 ruff에 넘기지 않는다
    assert ruff_runner.rule_exists("F401", tool_dir=tmp_path) is True
    assert calls[0][-2:] == ["rule", "F401"]
