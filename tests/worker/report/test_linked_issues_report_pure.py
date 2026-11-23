"""결과 코멘트의 이슈 절 (docs/DESIGN.md 4.5 끝 "연결된 이슈 확인", 4.9): 결론 줄, 펼친 부분, 접는
블록의 문구."""

from typing import Final

from app.worker.report import report
from app.worker.report.linked_issues_report_pure import NONE, RECHECK_HINT, expanded, folded, title
from app.worker.rules.linked_issues_pure import IssueGap, LinkedIssues
from app.worker.rules.pr_description_pure import DescriptionUsage, PrDescription
from app.worker.testing.test_run_pure import PytestRun, ReportSummary

MISSING: Final = IssueGap(
    73, "missing",
    "닫힘 소식을 받으면 api가 바로 표시를 적어야 하는데, 이 PR은 워커 쪽만 고쳤습니다.",
    "닫힘 소식(웹훅)을 받은 api가 바로 닫힘 표시를 적습니다.",
)  # fmt: skip
DEPLOY_SAID: Final = "배포 뒤 운영 알림을 확인해 달라는 요구입니다."
DEPLOY: Final = IssueGap(75, "unverifiable", DEPLOY_SAID, "배포 뒤")
DEPLOY_LINE: Final = f"- 코드로 확인할 수 없음: `{DEPLOY_SAID}` (#75)"
USAGE: Final = DescriptionUsage(calls=1, tool_uses=4, input_tokens=9000, output_tokens=300,
                                models=("claude-sonnet",), dropped=1)  # fmt: skip


def test_resolved_is_one_line_with_the_issue_numbers() -> None:
    d = LinkedIssues("checked", issues=(73,), files=1, shown=1, usage=USAGE)
    assert title(d) == "**이슈:** #73이 요구한 것을 해결했습니다."
    assert expanded(d) == []
    assert title(LinkedIssues("checked", issues=(73, 75))) == (
        "**이슈:** #73, #75가 요구한 것을 해결했습니다."
    )
    assert title(LinkedIssues("checked", issues=(12,))).startswith("**이슈:** #12가 ")
    assert title(LinkedIssues("checked", issues=(10,))).startswith("**이슈:** #10이 ")


def test_missing_items_are_said_like_a_reviewer_with_their_issue() -> None:
    d = LinkedIssues("checked", issues=(73, 75), items=(DEPLOY, MISSING), files=1, shown=1)
    assert title(d) == "**이슈:** #73, #75가 요구한 것 중 빠진 곳이 1건 있습니다."
    assert expanded(d) == [
        "",
        f"- 이슈가 요구했는데 빠짐: `{MISSING.summary}` (#73)",
        DEPLOY_LINE,
        RECHECK_HINT,
    ]


def test_unverifiable_only_is_resolved_with_a_note() -> None:
    d = LinkedIssues("checked", issues=(75,), items=(DEPLOY,))
    assert title(d) == "**이슈:** #75가 요구한 것을 해결했습니다."
    assert expanded(d) == ["", DEPLOY_LINE]


def test_no_linked_issue_has_no_warning_and_nothing_else() -> None:
    d = LinkedIssues("none")
    assert title(d) == "**이슈:** 연결된 이슈가 없습니다." == NONE
    assert "⚠️" not in NONE and expanded(d) == [] and folded(d) == []
    assert title(None) is None and expanded(None) == [] and folded(None) == []


def test_could_not_compare_says_why() -> None:
    capped = LinkedIssues("unchecked", issues=(73,), gap="cap", detail="PR 상한 (3/3)")
    assert title(capped) == "**이슈:** 견주지 못했습니다 (모델 호출 상한에 걸림)."
    assert expanded(capped) == [
        "",
        "이슈와 견주지 못한 까닭: `PR 상한 (3/3)`",
        "다음 커밋의 검사나 `/recheck`가 다시 견줍니다.",
    ]
    unread = LinkedIssues("unchecked", gap="unreadable", detail="연결된 이슈를 읽지 못함: HTTP 403")
    assert title(unread) == "**이슈:** 견주지 못했습니다 (이슈를 읽지 못함)."
    assert expanded(unread) == ["", "이슈와 견주지 못한 까닭: `연결된 이슈를 읽지 못함: HTTP 403`"]
    other = LinkedIssues("unchecked", gap="other_repo", other_repo=("x/`y`#3",))
    assert title(other) == "**이슈:** 견주지 못했습니다 (다른 저장소의 이슈만 연결됨)."
    assert expanded(other) == ["", "- 다른 저장소의 이슈라 보지 않음: ``x/`y`#3``"]


def test_what_was_not_seen_is_noted() -> None:
    d = LinkedIssues("checked", issues=(1, 2, 3, 4, 5), other_repo=("x/y#9",), over=2,
                     unreadable=1, text_cut=True, files=3, shown=2)  # fmt: skip
    lines = expanded(d)
    assert "- 다른 저장소의 이슈라 보지 않음: `x/y#9`" in lines
    assert lines[-1] == (
        "연결된 이슈가 많아 앞의 5개만 견줬습니다 (보지 않은 이슈 2개). "
        "읽지 못한 연결된 이슈 1개는 보지 않았습니다. "
        "변경이 커서 바뀐 파일 3개 중 1개는 diff를 다 보이지 못했습니다. "
        "그 파일은 모델이 필요할 때 작업 폴더에서 읽었습니다. "
        "이슈 글이나 댓글이 길어 일부만 견줬습니다."
    )


def test_usage_goes_to_the_folded_block() -> None:
    d = LinkedIssues("checked", issues=(73,), hidden=2, usage=USAGE)
    assert folded(d) == [
        "",
        "이슈 확인: 모델 호출 1회 · 읽기 도구 4회 · 저장된 결과 0건 사용 · `claude-sonnet` · "
        "버린 답 1건 (이슈 글을 그대로 인용하지 않음) · 토큰 입력 9000, 출력 300 · "
        "저장소 사람이 아닌 댓글 2개는 읽지 않음",
    ]
    cached = LinkedIssues("checked", issues=(73,), usage=DescriptionUsage(cached=1))
    assert folded(cached) == ["", "이슈 확인: 모델 호출 0회 · 저장된 결과 1건 사용"]


def test_issue_line_sits_under_the_pr_description_line() -> None:
    run = PytestRun(exit_code=0, summary=ReportSummary(passed=1, total=1))
    body = report.render_result(
        run, "a" * 40, 1.0, description=PrDescription("checked", files=1, shown=1),
        issues=LinkedIssues("checked", issues=(73,), items=(MISSING,), files=1, shown=1,
                            usage=USAGE),
    )  # fmt: skip
    lines = body.splitlines()
    at = lines.index("**PR 설명:** 설명한 내용과 변경이 맞습니다.")
    assert lines[at + 1] == "**이슈:** #73이 요구한 것 중 빠진 곳이 1건 있습니다."
    assert f"- 이슈가 요구했는데 빠짐: `{MISSING.summary}` (#73)" in lines
    assert any(line.startswith("이슈 확인: 모델 호출 1회") for line in lines)
    off = report.render_result(run, "a" * 40, 1.0)
    assert "**이슈:**" not in off
