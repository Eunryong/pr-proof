"""app/worker/ai/path_hook.py: 도구가 가리키는 경로의 실제 위치가 작업 폴더 밖이면 거부한다."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.worker.ai import path_hook


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    root, outside = (tmp_path / "work").resolve(), (tmp_path / "outside").resolve()
    (root / "pkg").mkdir(parents=True)
    outside.mkdir()
    (root / "pkg" / "a.py").write_text("x = 1\n")
    (outside / "secret.txt").write_text("secret\n")
    return root, outside


def _problem(root: Path, tool: str, **tool_input: object) -> str | None:
    return path_hook.problem(str(root), {"tool_name": tool, "tool_input": tool_input})


def test_paths_inside_are_allowed(dirs) -> None:
    root, _ = dirs
    assert _problem(root, "Read", file_path=str(root / "pkg" / "a.py")) is None
    assert _problem(root, "Read", file_path="pkg/a.py") is None  # 상대 경로는 작업 폴더 기준
    assert _problem(root, "Write", file_path=str(root / "pkg" / "new.py")) is None  # 아직 없는 파일
    assert _problem(root, "Edit", file_path=str(root / "pkg" / "a.py")) is None
    assert _problem(root, "Grep", pattern="x") is None  # 경로가 없으면 작업 폴더
    assert _problem(root, "Grep", pattern="x", path=str(root), glob="**/*.py") is None
    assert _problem(root, "Glob", pattern="pkg/**/*.py") is None


def test_outside_paths_are_denied(dirs) -> None:
    root, outside = dirs
    secret = str(outside / "secret.txt")
    for tool in ("Read", "Write", "Edit"):
        assert _problem(root, tool, file_path=secret) is not None
    assert _problem(root, "Read", file_path="../outside/secret.txt") is not None
    assert _problem(root, "Read", file_path=str(root / ".." / "outside" / "secret.txt")) is not None
    assert _problem(root, "Read", file_path=str(root) + "-sibling/x") is not None  # 앞부분만 같음
    assert _problem(root, "Grep", pattern="x", path=str(outside)) is not None
    assert _problem(root, "Glob", pattern="*", path="/") is not None


def test_symlinks_are_judged_by_where_they_really_point(dirs) -> None:
    root, outside = dirs
    (root / "notes.txt").symlink_to(outside / "secret.txt")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "inner").symlink_to(root / "pkg", target_is_directory=True)
    assert _problem(root, "Read", file_path=str(root / "notes.txt")) is not None
    assert _problem(root, "Read", file_path=str(root / "linked" / "secret.txt")) is not None
    assert _problem(root, "Write", file_path=str(root / "linked" / "new.txt")) is not None
    assert _problem(root, "Grep", pattern="x", path=str(root / "linked")) is not None
    assert _problem(root, "Glob", pattern="linked/*") is not None
    # 폴더 안을 가리키는 링크는 실제 위치가 안이다
    assert _problem(root, "Read", file_path=str(root / "inner" / "a.py")) is None


def test_patterns_that_look_outside_are_denied(dirs) -> None:
    root, outside = dirs
    assert _problem(root, "Glob", pattern="../**/*") is not None
    assert _problem(root, "Glob", pattern=f"{outside}/*") is not None
    assert _problem(root, "Glob", pattern="pkg/../../outside/*") is not None
    assert _problem(root, "Grep", pattern="x", glob="../*") is not None
    assert _problem(root, "Glob", pattern=f"{root}/pkg/*.py") is None


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        {"tool_name": "Bash", "tool_input": {"command": "cat /etc/passwd"}},
        {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
        {"tool_name": "Read"},
        {"tool_name": "Read", "tool_input": {}},
        {"tool_name": "Read", "tool_input": {"file_path": ""}},
        {"tool_name": "Read", "tool_input": {"file_path": 3}},
        {"tool_name": "Read", "tool_input": {"file_path": "a\0b"}},
        {"tool_name": "Glob", "tool_input": {"pattern": 3}},
    ],
)
def test_unknown_tools_and_malformed_input_are_denied(dirs, event) -> None:
    assert path_hook.problem(str(dirs[0]), event) is not None


def _run_hook(root: Path, log: Path, stdin: str) -> subprocess.CompletedProcess[str]:
    script = Path(path_hook.__file__)
    return subprocess.run(
        [sys.executable, "-I", str(script), str(root), str(log)],
        input=stdin, capture_output=True, text=True, check=False, env={"PATH": os.defpath},
    )  # fmt: skip


def test_script_denies_with_the_hook_protocol_and_logs_every_call(dirs, tmp_path: Path) -> None:
    root, outside = dirs
    log = tmp_path / "hook.log"
    allowed = {"tool_name": "Read", "tool_input": {"file_path": str(root / "pkg" / "a.py")}}
    denied = {"tool_name": "Read", "tool_input": {"file_path": str(outside / "secret.txt")}}

    ok = _run_hook(root, log, json.dumps(allowed))
    assert (ok.returncode, ok.stdout) == (0, "")
    no = _run_hook(root, log, json.dumps(denied))
    decision = json.loads(no.stdout)["hookSpecificOutput"]
    assert no.returncode == 0 and decision["permissionDecision"] == "deny"
    assert decision["hookEventName"] == "PreToolUse"

    lines = [json.loads(line) for line in log.read_text().splitlines()]
    assert [line["denied"] is None for line in lines] == [True, False]
    assert "secret" not in log.read_text()  # 기록에는 경로나 내용을 남기지 않는다


def test_script_blocks_when_it_cannot_decide(dirs, tmp_path: Path) -> None:
    root, _ = dirs
    broken = _run_hook(root, tmp_path / "hook.log", "not json")
    assert broken.returncode == 2 and broken.stdout == ""
    unwritable = _run_hook(root, tmp_path / "no-such-dir" / "hook.log", "{}")
    assert unwritable.returncode == 2


@pytest.mark.parametrize("tool", ["mcp__proof__check_rules", "mcp__proof__run_tests"])
def test_pipeline_tools_are_allowed_without_a_path(dirs, tool: str) -> None:
    root, _ = dirs
    assert (
        path_hook.problem(str(root), {"tool_name": tool, "tool_input": {"code": "before"}}) is None
    )


def test_hook_knows_the_pipeline_tool_names() -> None:
    from app.worker.mcp_pure import TOOL_NAMES

    assert set(TOOL_NAMES) <= set(path_hook._PATH_KEYS)
    assert path_hook.problem("/x", {"tool_name": "mcp__other__x", "tool_input": {}}) is not None
