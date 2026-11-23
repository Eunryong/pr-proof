"""모델의 직접 판단이 그래프의 상태로 남는지 (docs/DESIGN.md 4.5)."""

from typing import Final

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.worker import graph
from app.worker.checker_authoring_pure import ToJudge, Unchecked
from app.worker.doc_rules_pure import CallUsage, Found, RuleJudged, RuleUnchecked
from app.worker.repo_config_pure import ModelSettings
from app.worker.rule_pure import CheckMethod, Rule, RuleSource, rule_id
from app.worker.rules import doc_rules
from app.worker.rules.judge_pure import JudgeUsage

NAME_RULE: Final = "함수 이름은 하는 일을 동사로 시작한다."
NAME_ID: Final = rule_id("README.md", NAME_RULE)


def test_judged_rule_state_survives_checkpoint() -> None:
    rule = Rule(id=NAME_ID, text=NAME_RULE, source=RuleSource("README.md", "코드 규칙 > 이름", "b"),
                origin="document", scope=(), check=CheckMethod("checker"),
                fix="model")  # fmt: skip
    usage = JudgeUsage(rules=1, hunks=2, calls=1, cached=1, dropped=0, models=("m",))
    judged = RuleJudged(rule, "주석의 뜻", "none", 1, "cap", "PR 상한")
    found = Found((judged,), (), 0, CallUsage(0, 10, 0, (), 0, 0), judge=usage)
    serde = JsonPlusSerializer(allowed_msgpack_modules=list(graph._STATE_TYPES))
    restored = serde.loads_typed(serde.dumps_typed({"doc_rules": found}))["doc_rules"]
    (outcome,) = restored.outcomes
    assert isinstance(outcome, RuleJudged) and outcome.left_kind == "cap"
    assert outcome.route == "none"
    assert isinstance(restored.judge, JudgeUsage) and restored.judge.cached == 1


def test_routed_rule_becomes_judged_with_its_route_and_path_rules_stay_unchecked() -> None:
    rule = Rule(id=NAME_ID, text=NAME_RULE, source=RuleSource("README.md", "코드 규칙 > 이름", "b"),
                origin="document", scope=(), check=CheckMethod("checker"),
                fix="model")  # fmt: skip
    models = ModelSettings()
    judged = doc_rules._outcome(ToJudge(rule, "part", "뜻을 읽어야 한다"), models, None)
    assert judged == RuleJudged(rule, "뜻을 읽어야 한다", "part")
    path_rule = Rule(id="R-p", text="테스트 파일 이름은 test_로 시작한다.",
                     source=RuleSource("README.md", "테스트", "b"), origin="document",
                     scope=(), check=CheckMethod("path"), fix="none")  # fmt: skip
    left = doc_rules._outcome(Unchecked(path_rule, "no_checker", "3회 실패"), models, None)
    assert left == RuleUnchecked(path_rule, "no_checker", "3회 실패")
    # 수정 뒤 재검사(4.8의 5번)는 모델 판단으로 가는 규칙을 도구·검사기로 다시 돌리지 않는다
    found = Found((judged,), (), 0, CallUsage(0, 10, 0, (), 0, 0))
    assert doc_rules.saved_checks(found, store=None) == [ToJudge(rule, "part", "뜻을 읽어야 한다")]
