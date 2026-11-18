import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from app.models.finding import Finding
from app.worker.coverage import (
    DiffCoverage,
    annotate_coverage,
    diff_coverage,
    read_coverage,
    read_regular_file,
    read_sources,
)

COVERAGE_XML: Final = b"""<?xml version="1.0" ?>
<coverage version="7.6">
  <sources><source></source><source>.</source></sources>
  <packages><package name="."><classes>
    <class name="m.py" filename="m.py"><lines>
      <line number="1" hits="1"/>
      <line number="2" hits="1"/>
      <line number="4" hits="0"/>
      <line number="5" hits="0"/>
    </lines></class>
  </classes></package></packages>
</coverage>"""
SOURCE: Final = "def f(x):\n    return (x +\n            1)\ndef g():\n    return 2\n"
# m.py의 2–3줄(덮임 2)과 4–5줄(안 덮임 2), 문서 파일 1줄을 바꾼 diff
DIFF: Final = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,0 +2,4 @@
+    return (x +
+            1)
+def g():
+    return 2
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,0 +2,1 @@
+hello
"""


def _write(tmp_path: Path, xml: bytes = COVERAGE_XML, diff: str = DIFF) -> tuple[Path, Path]:
    out = tmp_path / ".pipeline-out"
    out.mkdir(exist_ok=True)
    (out / "coverage.xml").write_bytes(xml)
    (out / "changes.diff").write_text(diff)
    return out / "coverage.xml", out / "changes.diff"


def test_read_regular_file_size_boundary(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abcd")
    assert read_regular_file(path, max_bytes=4) == b"abcd"
    assert read_regular_file(path, max_bytes=3) is None


def test_read_regular_file_rejects_symlink_fifo_and_missing(tmp_path: Path) -> None:
    target = tmp_path / "secret"
    target.write_bytes(b"x")
    (tmp_path / "link").symlink_to(target)
    os.mkfifo(tmp_path / "fifo")  # 따라가면 쓰는 쪽이 없어 읽기가 멈춘다
    assert read_regular_file(tmp_path / "link", max_bytes=10) is None
    assert read_regular_file(tmp_path / "fifo", max_bytes=10) is None
    assert read_regular_file(tmp_path / "missing", max_bytes=10) is None
    assert read_regular_file(tmp_path, max_bytes=10) is None  # 폴더


def test_read_coverage_missing_or_broken_is_unknown(tmp_path: Path) -> None:
    xml, _ = _write(tmp_path, xml=b"<coverage><broken")
    assert read_coverage(None) is None
    assert read_coverage(tmp_path / "nope.xml") is None
    assert read_coverage(xml) is None


def test_read_sources_stays_inside_worktree(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / "pkg" / "a.py").write_text("x = 1\n")
    (worktree / "bin.py").write_bytes(b"\xff\xfe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.py").write_text("secret\n")
    (worktree / "out").symlink_to(outside)  # 중간 경로가 worktree 밖을 가리킨다
    files = ["pkg/a.py", "pkg/a.py", "out/b.py", "../outside/b.py", str(outside / "b.py")]
    files += ["bin.py", "missing.py"]
    assert read_sources(worktree, files) == {"pkg/a.py": "x = 1\n"}


def test_read_sources_size_limit(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    assert read_sources(tmp_path, ["a.py"], max_bytes=5) == {}


def _finding(line: int) -> Finding:
    return Finding("m.py", line, "warning", "w", "static", "기본 규칙")


def test_annotate_coverage_uses_source_for_continuation_lines(tmp_path: Path) -> None:
    xml, _ = _write(tmp_path)
    (tmp_path / "m.py").write_text(SOURCE)
    marked = annotate_coverage([_finding(3), _finding(5)], xml, tmp_path)
    assert [f.covered for f in marked] == [True, False]


def test_annotate_coverage_without_xml_is_unknown(tmp_path: Path) -> None:
    marked = annotate_coverage([_finding(3)], None, tmp_path)
    assert marked[0].covered is None


def test_diff_coverage_counts_measured_changed_lines(tmp_path: Path) -> None:
    xml, diff = _write(tmp_path)
    assert diff_coverage(xml, diff) == DiffCoverage(
        percent=33.33, measured_lines=3, uncovered_lines=2
    )


def test_diff_coverage_ignores_user_git_config(tmp_path: Path, monkeypatch) -> None:
    # 사용자 설정이 git을 망가뜨려도 diff-cover의 git 호출은 영향을 받지 않는다
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[core\n")  # 문법 오류: 이 설정을 읽는 git은 실패한다
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    broken = subprocess.run(["git", "config", "--list"], capture_output=True, check=False)
    assert broken.returncode != 0
    xml, diff = _write(tmp_path)
    assert diff_coverage(xml, diff) is not None


@pytest.mark.parametrize(
    ("xml", "diff"),
    [
        (b"<coverage><broken", DIFF),
        (b'<?xml version="1.0"?><!DOCTYPE c [<!ENTITY a "a">]><coverage>&a;</coverage>', DIFF),
        # 바뀐 줄이 모두 coverage가 세지 않는 줄이다 (문서 파일만 바꿈)
        (COVERAGE_XML, DIFF.split("diff --git a/README.md")[0].replace("m.py", "x.py")),
        (COVERAGE_XML, ""),
    ],
    ids=["broken-xml", "dtd", "no-measured-lines", "empty-diff"],
)
def test_diff_coverage_not_computable(tmp_path: Path, xml: bytes, diff: str) -> None:
    xml_path, diff_path = _write(tmp_path, xml=xml, diff=diff)
    assert diff_coverage(xml_path, diff_path) is None


def test_diff_coverage_missing_inputs(tmp_path: Path) -> None:
    xml, diff = _write(tmp_path)
    assert diff_coverage(None, diff) is None
    assert diff_coverage(xml, tmp_path / "missing.diff") is None


def test_diff_coverage_tool_failure_or_timeout(tmp_path: Path) -> None:
    xml, diff = _write(tmp_path)
    assert diff_coverage(xml, diff, python=str(tmp_path / "no-python")) is None
    assert diff_coverage(xml, diff, timeout=0.001) is None
    assert diff_coverage(xml, diff, python=sys.executable) is not None
