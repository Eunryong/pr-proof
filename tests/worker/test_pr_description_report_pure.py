"""결과 코멘트의 PR 설명 절 (docs/DESIGN.md 4.5 끝, 4.9): 결론 줄, 펼친 부분, 접는 블록의 문구."""

from typing import Final

from app.worker import report
from app.worker.pr_description_report_pure import (
    EMPTY,
    MATCHED,
    RECHECK_HINT,
    expanded,
    folded,
    title,
)
from app.worker.rules.pr_description_pure import DescriptionUsage, Mismatch, PrDescription
from app.worker.testing.test_run_pure import PytestRun, ReportSummary

ITEMS: Final = (
    Mismatch("unrelated", "config.py에서 기본 타임아웃을 30초에서 5초로 줄였습니다. 설명에는 파서 "
             "수정만 적혀 있습니다.", file="config.py", side="new", line=3, code="TIMEOUT = 5"),
    Mismatch("missing", '"오류 메시지를 한국어로 바꿨다"고 했지만 바뀐 메시지를 찾지 못했습니다.',
             claim="오류 메시지를 한국어로 바꿨다"),
    Mismatch("different", "재시도를 3회로 한다고 했는데 코드는 5회입니다.", claim="재시도 3회",
             file="client.py", side="new", line=88, code="RETRIES = 5"),
)  # fmt: skip
USAGE: Final = DescriptionUsage(calls=1, tool_uses=3, input_tokens=23000, output_tokens=500,
                         models=("claude-sonnet",), dropped=1)  # fmt: skip


def test_matching_description_is_one_line_and_nothing_expanded() -> None:
    d = PrDescription("checked", files=2, shown=2, usage=USAGE)
    assert title(d) == "**PR 설명:** 설명한 내용과 변경이 맞습니다." == MATCHED
    assert expanded(d) == []


def test_mismatches_are_said_like_a_reviewer() -> None:
    d = PrDescription("checked", ITEMS, files=2, shown=2)
    assert title(d) == "**PR 설명:** 설명과 다른 곳이 3건 있습니다."
    lines = expanded(d)
    assert lines[:2] == ["", "**PR 설명과 다른 곳:**"]
    assert lines[2] == (
        "- PR과 관련 없는 수정: `config.py에서 기본 타임아웃을 30초에서 5초로 줄였습니다. 설명에는 "
        "파서 수정만 적혀 있습니다.` (`config.py:3`)"
    )
    assert lines[3] == (
        '- 설명에는 있는데 코드에 없음: `"오류 메시지를 한국어로 바꿨다"고 했지만 바뀐 '
        "메시지를 찾지 못했습니다.`"
    )
    assert lines[4] == (
        "- 설명과 다름: `재시도를 3회로 한다고 했는데 코드는 5회입니다.` (`client.py:88`)"
    )
    assert lines[5] == RECHECK_HINT
    assert not any("unrelated" in line or "missing" in line for line in lines)


def test_model_text_cannot_mention_or_mark_up() -> None:
    evil = Mismatch("unrelated", "@owner `x` <b>y</b>\n## 제목", file="a`b.py", side="old", line=2,
                    code="x")  # fmt: skip
    (line,) = [x for x in expanded(PrDescription("checked", (evil,))) if x.startswith("- ")]
    assert line == "- PR과 관련 없는 수정: ``@owner `x` <b>y</b> ## 제목`` (``a`b.py:2``, 지운 줄)"


def test_empty_and_unchecked_say_why() -> None:
    assert (
        title(PrDescription("empty")) == "**PR 설명:** 설명이 비어 있어 견주지 못했습니다." == EMPTY
    )
    assert "`/recheck`" in expanded(PrDescription("empty"))[1]
    capped = PrDescription("unchecked", gap="cap", detail="PR 상한 20회에 닿음")
    assert title(capped) == "**PR 설명:** 견주지 못했습니다 (모델 호출 상한에 걸림)."
    assert expanded(capped) == [
        "", "PR 설명을 견주지 못한 까닭: `PR 상한 20회에 닿음`",
        "다음 커밋의 검사나 `/recheck`가 다시 견줍니다.",
    ]  # fmt: skip
    failed = PrDescription("unchecked", gap="model_failed", detail="timeout", usage=USAGE)
    assert title(failed) == "**PR 설명:** 견주지 못했습니다 (모델 호출이 실패함)."
    assert title(PrDescription("unchecked", gap="no_facts")) == (
        "**PR 설명:** 견주지 못했습니다 (PR 글을 읽지 못함)."
    )
    assert title(None) is None and expanded(None) == [] and folded(None) == []


def test_cut_diff_and_text_are_noted() -> None:
    lines = expanded(PrDescription("checked", files=5, shown=3, text_cut=True))
    assert lines == [
        "",
        "변경이 커서 바뀐 파일 5개 중 2개는 diff를 다 보이지 못했습니다. 그 파일은 모델이 "
        "필요할 때 작업 폴더에서 읽었습니다. PR 글이 길어 앞부분만 견줬습니다.",
    ]


def test_usage_goes_to_the_folded_block() -> None:
    assert folded(PrDescription("checked", usage=USAGE)) == [
        "", "PR 설명 확인: 모델 호출 1회 · 읽기 도구 3회 · 저장된 결과 0건 사용 · "
        "`claude-sonnet` · 버린 답 1건 (PR 글이나 바뀐 줄을 그대로 가리키지 않음) · "
        "토큰 입력 23000, 출력 500",
    ]  # fmt: skip
    cached = PrDescription("checked", usage=DescriptionUsage(cached=1))
    assert folded(cached) == ["", "PR 설명 확인: 모델 호출 0회 · 저장된 결과 1건 사용"]
    assert folded(PrDescription("empty")) == []
    again = DescriptionUsage(calls=1, earlier=1, earlier_items=2, kept_earlier=1)
    assert folded(PrDescription("checked", usage=again))[1].endswith(
        " · 지난 결과를 주고 다시 견줌 (지난 어긋남 2건 중 유지 1건)"
    )


def test_result_comment_puts_the_line_on_top_and_the_items_expanded() -> None:
    run = PytestRun(exit_code=0, summary=ReportSummary(passed=2, total=2))
    body = report.render_result(run, "a" * 40, 1.0, description=PrDescription("checked", ITEMS[:1],
                                usage=USAGE))  # fmt: skip
    lines = body.splitlines()
    assert lines[2:4] == ["**테스트:** 통과 (2개)", "**PR 설명:** 설명과 다른 곳이 1건 있습니다."]
    assert body.index("**PR 설명과 다른 곳:**") < body.index("<details>")
    assert body.index("PR 설명 확인: 모델 호출 1회") > body.index("<details>")
    assert "PR 설명" not in report.render_result(run, "a" * 40, 1.0)
