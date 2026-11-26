"""PR 설명 확인: 변경이 커서 묶음으로 나눠 견줄 때의 프롬프트와 답 (docs/DESIGN.md 4.5 끝 "큰
변경")."""

from typing import Final

from app.worker.rules.diff_batches_pure import CHANGES_RULE, Batch, Split
from app.worker.rules.pr_description_batch_pure import (
    BATCH_SCHEMA,
    BATCH_SYSTEM,
    BATCH_VERSION,
    MISSING_SYSTEM,
    build_batch_prompt,
    build_missing_prompt,
    earlier_for_batch,
    earlier_missing,
    merge,
    only_kinds,
)
from app.worker.rules.pr_description_pure import (
    MAX_ITEMS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    AnswerItem,
    Described,
    Earlier,
    Mismatch,
)

TEXT: Final = Described("파서를 고친다", "빈 줄을 건너뛴다.", (("c" * 40, "파서 수정"),))
PARTS: Final = Split(
    (Batch("파일: a.py\nA", ("a.py", "big.py")), Batch("파일: big.py\nB", ("big.py", "c.py"))),
    ("x.lock",), 4,
)  # fmt: skip
IN_A: Final = Mismatch("unrelated", "a를 바꿈", file="a.py", side="new", line=1, code="x")
IN_C: Final = Mismatch("different", "c가 다름", "빈 줄을 건너뛴다.", "c.py", "new", 2, "y")
GONE: Final = Mismatch("missing", "없음", "빈 줄을 건너뛴다.")
EARLIER: Final = Earlier((IN_A, GONE, IN_C), "e" * 40, False, ("a.py",))


def test_batch_instructions_are_the_single_call_text_plus_what_differs() -> None:
    assert BATCH_SYSTEM.startswith(SYSTEM_PROMPT + "\n묶음: ")
    assert MISSING_SYSTEM.startswith(SYSTEM_PROMPT + "\n묶음: ")
    assert CHANGES_RULE in BATCH_SYSTEM and "{{" not in BATCH_SYSTEM
    assert "missing은 답하지 않는다" in BATCH_SYSTEM and "missing만 답한다" in MISSING_SYSTEM
    kinds = BATCH_SCHEMA["properties"]["mismatches"]["items"]["properties"]["kind"]
    assert kinds == {"enum": ["unrelated", "different"]}
    assert BATCH_SCHEMA["required"] == ["mismatches", "changes"]
    assert BATCH_VERSION != PROMPT_VERSION


def test_batch_prompt_shows_the_pr_text_its_diff_and_the_other_files() -> None:
    prompt = build_batch_prompt(
        TEXT, PARTS, 1, "수정안의 변경", earlier_for_batch(EARLIER, PARTS.batches[1])
    )
    assert "묶음 2개로 나눈 것 중 2번째다" in prompt
    for part in ("<<<PR_TITLE", "<<<PR_BODY", f"<<<COMMIT sha={'c' * 40}", "<<<PIPELINE_FIX"):
        assert part in prompt
    assert "\n파일: big.py\nB\n<<<END DIFF" in prompt and "\nA\n" not in prompt
    assert "\na.py\nx.lock\n<<<END OTHER_FILES" in prompt
    assert "1. different (c.py:2, new: y)" in prompt and "a를 바꿈" not in prompt
    assert "없음" not in prompt.split("<<<EARLIER")[1]


def test_missing_prompt_shows_the_digests_instead_of_the_diff() -> None:
    prompt = build_missing_prompt(
        TEXT, PARTS, [("a.py: x를 바꿈",), None], earlier_missing(EARLIER)
    )
    assert "<<<DIFF" not in prompt and "<<<PR_BODY" in prompt
    assert "묶음 1 (파일: a.py, big.py)\n- a.py: x를 바꿈\n" in prompt
    assert "- big.py: 변경 포함 (요약에서 경로 생략됨)\n<<<END CHANGES" in prompt
    assert "\nx.lock\nbig.py\nc.py\n<<<END NOT_SEEN" in prompt
    assert "1. missing PR 글: 빈 줄을 건너뛴다. — 없음" in prompt
    assert "a를 바꿈" not in prompt and "c가 다름" not in prompt
    tags = {line.split("tag=")[1] for line in prompt.splitlines() if line.startswith("<<<")}
    assert len(tags) == 1
    other = build_missing_prompt(TEXT, PARTS, [("a.py: 다른 요약",), None])
    assert tags.isdisjoint(ln.split("tag=")[1] for ln in other.splitlines() if "tag=" in ln)


def test_earlier_result_is_narrowed_to_what_each_call_answers() -> None:
    assert earlier_for_batch(EARLIER, PARTS.batches[0]).items == (IN_A,)
    assert earlier_for_batch(EARLIER, PARTS.batches[1]).items == (IN_C,)
    assert earlier_missing(EARLIER).items == (GONE,)
    assert earlier_missing(EARLIER).sha == EARLIER.sha
    assert earlier_for_batch(None, PARTS.batches[0]) is None and earlier_missing(None) is None


def test_each_call_keeps_only_the_kinds_it_answers() -> None:
    items = (AnswerItem(kind="unrelated"), AnswerItem(kind="missing"), AnswerItem(kind="different"))
    assert [i.kind for i in only_kinds(items, missing=False)] == ["unrelated", "different"]
    assert [i.kind for i in only_kinds(items, missing=True)] == ["missing"]


def test_merge_drops_repeats_across_batches_and_keeps_the_cap() -> None:
    again = Mismatch("unrelated", "같은 줄을 다른 묶음이 답함", file="a.py", side="new", line=1,
                     code="x")  # fmt: skip
    assert merge([(IN_A, IN_C), (again,), (GONE, GONE)]) == (IN_A, IN_C, GONE)
    many = tuple(Mismatch("unrelated", "s", file="a.py", side="new", line=n, code="x")
                 for n in range(MAX_ITEMS + 5))  # fmt: skip
    assert len(merge([many])) == MAX_ITEMS
