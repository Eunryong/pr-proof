"""수동 `/fix` 재시도·기존 수정안 PR 갱신 및 비-수정방법 실패 필터링 테스트 (pr-proof#149)."""

from app.shared.db_pure import Record
from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.core.violation_pure import Violation
from app.worker.fix import decide
from app.worker.fix.fix_decision_pure import Decided, FixDecision, SafetyNet
from app.worker.graph import MODEL, _rerun
from app.worker.rules.doc_rules_pure import CallUsage, Found
from app.worker.store import rag_pure, records_pure
from app.worker.store.rag_pure import Chosen, Selection


def _rule() -> Rule:
    return Rule(
        id="R-doc-12345678",
        text="print를 쓰지 않는다.",
        source=RuleSource("CONTRIBUTING.md", "규칙", "a" * 40),
        origin="document",
        scope=(),
        check=CheckMethod("checker"),
        fix="model",
    )


def _violation() -> Violation:
    return Violation(
        id="v1", rule_id="R-doc-12345678", file="a.py", line=3, message="print", found_by="checker"
    )


def _record(*, id_: int, kind: str, pr_number: int, detail: str = "") -> Record:
    return Record(
        id=id_,
        repo="o/r",
        kind=kind,
        origin="verification",
        source_id=f"s:{id_}",
        rule_id="R-doc-12345678",
        rule_text="print를 쓰지 않는다.",
        file="a.py",
        line_text="print(1)",
        context="print(1)",
        diff="",
        detail=detail,
        pr_number=pr_number,
    )


def test_format_failure_detail_tags_remaining_violation_and_status() -> None:
    assert records_pure.format_failure_detail("남은 위반 1건", "failed").startswith("[남은 위반]")
    assert records_pure.format_failure_detail("pytest fail", "tests_changed").startswith(
        "[테스트 실패]"
    )
    assert records_pure.format_failure_detail("범위 밖", "out_of_scope").startswith(
        "[범위 밖 변경]"
    )


def test_filter_decide_records_drops_same_pr_non_method_failures_only() -> None:
    same_pr_remaining = Chosen(
        _record(id_=1, kind="failed", pr_number=10, detail="[남은 위반] 남은 위반 1건"), 0.98
    )
    same_pr_test_fail = Chosen(
        _record(id_=2, kind="failed", pr_number=10, detail="[테스트 실패] assert 1 == 2"), 0.95
    )
    other_pr_remaining = Chosen(
        _record(id_=3, kind="failed", pr_number=9, detail="[남은 위반] 남은 위반 1건"), 0.92
    )
    same_pr_rejected = Chosen(_record(id_=4, kind="rejected", pr_number=10, detail=""), 0.90)
    sel = Selection((same_pr_remaining, same_pr_test_fail, other_pr_remaining, same_pr_rejected))

    filtered = rag_pure.filter_decide_records(sel, current_pr=10)
    assert [c.record.id for c in filtered.chosen] == [2, 3, 4]


def test_render_records_distinguishes_same_pr_and_other_pr() -> None:
    same = Chosen(_record(id_=1, kind="failed", pr_number=10, detail="[테스트 실패] fail"), 0.95)
    other = Chosen(_record(id_=2, kind="adopted", pr_number=7, detail=""), 0.90)
    rendered = rag_pure.render_records((same, other), current_pr=10)
    assert "같은 PR #10" in rendered
    assert "다른 PR #7" in rendered


def test_manual_fix_bypasses_model_skip_and_marks_model_targets_for_fix() -> None:
    rule = _rule()
    v = _violation()
    found = Found(
        outcomes=(),
        violations=(v,),
        disabled=0,
        usage=CallUsage(0, 10, 0, (), 0, 0),
    )
    # outcomes_by_rule needs an outcome for rule
    from app.worker.rules.doc_rules_pure import RuleJudged

    found = Found(
        outcomes=(RuleJudged(rule=rule, reason=""),),
        violations=(v,),
        disabled=0,
        usage=CallUsage(0, 10, 0, (), 0, 0),
    )

    def _never_ask(*a, **k):
        raise AssertionError("manual /fix에서는 모델 skip 판단을 호출하지 않는다")

    result = decide.decide(
        found,
        SafetyNet("passed"),
        b"",
        store=None,  # type: ignore[arg-type]
        budget=None,  # type: ignore[arg-type]
        model="sonnet",
        ask=_never_ask,
        manual=True,
        current_pr=10,
    )
    assert len(result.decisions) == 1
    assert result.decisions[0].action == "fix"
    assert result.decisions[0].method == "model"


def test_rerun_allows_model_when_skipped_or_open_but_blocks_merged() -> None:
    skipped = Decided((FixDecision("v1", "skip", "model", reason="risky", why="지난 실패"),))
    ready_model = Decided((FixDecision("v1", "fix", "model", method="model"),))
    table_only = Decided((FixDecision("v1", "skip", "pipeline", reason="notify_only"),))

    assert _rerun({"fix_decisions": skipped}, MODEL) is True
    assert _rerun({"fix_decisions": ready_model}, MODEL, blocked="open") is True
    assert _rerun({"fix_decisions": ready_model}, MODEL, blocked="merged") is False
    assert _rerun({"fix_decisions": table_only}, MODEL) is False
