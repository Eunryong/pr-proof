"""app/worker/fix/mechanical_fix.py: 도구의 편집 적용과 대상 고르기. 흐름 전체는
test_mechanical_flow.py."""

from pathlib import Path

from app.worker.fix import mechanical_fix
from app.worker.fix.fix_decision_pure import FixDecision
from app.worker.text_edit_pure import TextEdit
from app.worker.violation_pure import Violation


def _violation(vid: str, unit: str = "line") -> Violation:
    return Violation(id=vid, rule_id="F401", file="a.py", line=1, message="m", found_by="tool",
                     code="F401", mechanical_fix=True, unit=unit)  # fmt: skip


def test_targets_are_only_mechanical_fix_decisions_on_lines() -> None:
    violations = (_violation("a"), _violation("b"), _violation("c"), _violation("d", "file"))
    decisions = (
        FixDecision("a", "fix", "pipeline", method="mechanical"),
        FixDecision("b", "fix", "model", method="model"),
        FixDecision("c", "skip", "pipeline", reason="notify_only"),
        FixDecision("d", "fix", "pipeline", method="mechanical"),
    )
    assert [v.id for v in mechanical_fix.pick_targets(violations, decisions).targets] == ["a"]


def test_edits_never_write_through_links_or_outside(tmp_path: Path) -> None:
    """편집은 worktree 안의 일반 파일에만 쓴다. 샌드박스가 돈 뒤 PR 코드가 남긴 링크를 따라가지
    않는다 (T4)."""
    outside = tmp_path / "outside.py"
    outside.write_text("import os\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "link.py").symlink_to(outside)
    (wt / "pkg").symlink_to(tmp_path, target_is_directory=True)  # 상위 폴더가 링크
    (wt / "a.py").write_text("import os\r\nx = 1\r\n")
    edit = (TextEdit(1, 1, 2, 1, ""),)
    touched: set[str] = set()
    groups = {"link.py": [edit], "pkg/outside.py": [edit], "a.py": [edit]}
    applied = mechanical_fix._apply(wt, groups, touched)
    assert outside.read_text() == "import os\n"
    assert (wt / "a.py").read_bytes() == b"x = 1\r\n"  # 줄바꿈을 그대로 둔다
    assert applied == 1 and touched == {"a.py"}
