"""큰 diff 적응형 처리, 요약 경로 보완, 미호출 신규 심볼 팩트 추출 테스트 (pr-proof#156)."""

from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.model.local.local_backend import LocalOpenAIBackend
from app.worker.rules.diff_batches_pure import Batch, Split, changes_blocks, ensure_all_paths, split
from app.worker.rules.pr_description_pure import (
    Described,
    build_prompt,
    parse_diff,
    render_view,
    unwired_symbol_facts,
)


def test_backend_capabilities_expose_model_appropriate_diff_limits() -> None:
    claude_cap = ClaudeCliBackend.capabilities.max_diff_chars
    local_cap = LocalOpenAIBackend.capabilities.max_diff_chars
    assert claude_cap >= 300_000
    assert local_cap < claude_cap


def test_large_diff_fits_single_call_under_claude_limit() -> None:
    diff = (
        "diff --git a/models/reservation.py b/models/reservation.py\n"
        "--- a/models/reservation.py\n+++ b/models/reservation.py\n"
        "@@ -1,1 +1,10002 @@\n HOLD_MINUTES = 15\n+" + ("x = 1\n+" * 10_000) + "y = 2\n"
    )
    files = parse_diff(diff)
    assert not split(files, limit=120_000).single
    assert split(files, limit=ClaudeCliBackend.capabilities.max_diff_chars).single


def test_ensure_all_paths_preserves_every_file_in_summary() -> None:
    paths = tuple(f"pkg/mod_{i}.py" for i in range(50))
    batch = Batch("diff", paths)
    # 모델이 50개 파일 중 일부만 요약에 적어도 결정적으로 모든 경로가 남는다
    model_summary = [f"pkg/mod_{i}.py: 함수 추가" for i in range(36)]
    completed = ensure_all_paths(batch, model_summary)
    joined = "\n".join(completed)
    for p in paths:
        assert p in joined

    blocks = changes_blocks(Split((batch,), (), 50), [model_summary], "tag")
    assert all(p in blocks[0] for p in paths)


def test_unwired_symbol_facts_flags_prod_symbol_only_referenced_in_tests() -> None:
    diff = (
        "diff --git a/orders/shipping.py b/orders/shipping.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n+++ b/orders/shipping.py\n"
        "@@ -0,0 +1,6 @@\n"
        "+def delivery_window(region: str) -> tuple[int, int]:\n"
        "+    return (1, 2)\n"
        "+\n"
        "+def quote_date(region: str) -> int:\n"
        "+    return 1\n"
    )
    sources = {
        "orders/shipping.py": (
            "def delivery_window(region: str) -> tuple[int, int]:\n"
            "    return (1, 2)\n\n"
            "def quote_date(region: str) -> int:\n"
            "    return 1\n"
        ),
        "orders/api.py": "from orders.shipping import quote_date\nRESULT = quote_date('jeju')\n",
        "tests/test_shipping.py": (
            "from orders.shipping import delivery_window\n"
            "assert delivery_window('jeju') == (1, 2)\n"
        ),
    }
    files = parse_diff(diff)
    facts = unwired_symbol_facts(files, sources)
    assert len(facts) == 1
    assert "`delivery_window`" in facts[0]
    assert "프로덕션 코드 참조 0건" in facts[0]
    assert "테스트 참조 2건" in facts[0]

    prompt = build_prompt(
        Described("도서 산간 배송", "도서 산간은 하루 범위", ()),
        render_view(files),
        static_facts=facts,
    )
    assert "<<<STATIC_FACTS" in prompt and "`delivery_window`" in prompt
