"""수정안 품질 게이트(인접 모듈 공개 함수 시그니처 주입, 분기 증가 감지) 및 로컬 모델 검사기
작성 조기 탈출 테스트."""

from pathlib import Path

from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.core.violation_pure import Violation
from app.worker.fix import model_fix, model_fix_pure
from app.worker.fix.model_fix_pure import Target
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.model.local.local_backend import LocalOpenAIBackend


def test_sibling_helpers_extracts_public_signatures_and_skips_private_and_tests(
    tmp_path: Path,
) -> None:
    orders = tmp_path / "orders"
    orders.mkdir()
    (orders / "order.py").write_text("def subtotal(order):\n    return 0\n")
    (orders / "pricing.py").write_text(
        "def calculate_total(items, discount=0):\n    return 0\n\n"
        "def _internal_helper():\n    pass\n"
    )
    (orders / "test_pricing.py").write_text("def test_x():\n    pass\n")

    helpers = model_fix.sibling_helpers(tmp_path, ["orders/order.py"])
    assert helpers == {"orders/pricing.py": ["calculate_total(items, discount)"]}


def test_build_prompt_includes_sibling_helpers_hint() -> None:
    rule = Rule(
        id="R-doc-1",
        text="금액 계산은 pricing 모듈을 사용한다.",
        source=RuleSource("CONTRIBUTING.md", "규칙", "a" * 40),
        origin="document",
        scope=(),
        check=CheckMethod("checker"),
        fix="model",
    )
    v = Violation(
        id="v1",
        rule_id="R-doc-1",
        file="orders/order.py",
        line=2,
        message="dup",
        found_by="checker",
    )
    prompt = model_fix_pure.build_prompt(
        [Target(v, rule)], helpers={"orders/pricing.py": ["calculate_total(items, discount)"]}
    )
    assert "같은 폴더 모듈의 기존 공개 함수" in prompt
    assert "orders/pricing.py: calculate_total(items, discount)" in prompt


def test_added_branch_count_detects_extra_if_and_try_blocks() -> None:
    before = "def avg(value, count):\n    return value / count\n"
    minimal = "def avg(value, count):\n    return value // count\n"
    over_engineered = (
        "def avg(value, count):\n    if value < 0:\n        return -(-value // count)\n"
        "    return value // count\n"
    )
    assert model_fix_pure.added_branch_count(before, minimal) == 0
    assert model_fix_pure.added_branch_count(before, over_engineered) == 1


def test_local_backend_uses_early_bailout_for_checker_authoring() -> None:
    assert LocalOpenAIBackend.capabilities.max_checker_attempts == 1
    assert ClaudeCliBackend.capabilities.max_checker_attempts == 3
