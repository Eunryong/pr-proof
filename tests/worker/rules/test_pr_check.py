"""app/worker/rules/pr_check.py: PR 단위 확인의 계획(저장, 회차, 모델 판단으로 보내기), PR 패턴,
바뀐 파일 조건, 모델 판단(저장, 상한, 실패), 사실을 읽지 못함 (4.5 끝, 4-11, 4-12).

샌드박스는 고정 스크립트(app/worker/sandbox/checker_runner.py의 PR 패턴)를 이 프로세스에서 그대로
돌리는 대역이다. 실제 컨테이너는 tests/worker/sandbox의 docker 테스트가 본다.
"""

from typing import Final

from app.worker.core.rule_pure import CheckMethod, Rule, RuleSource
from app.worker.rules import pr_check


def _rule(rid: str, text: str, examples=()) -> Rule:
    return Rule(id=rid, text=text, source=RuleSource("README.md", "기여", "0"), origin="document",
                scope=(), check=CheckMethod("none"), fix="none", target="pr",
                examples=tuple(examples))  # fmt: skip


COMMITS: Final = _rule("R-readme-commit", "커밋 메시지는 feat: 나 fix: 로 시작한다.")


def test_pr_rules_come_only_from_unchecked_pr_outcomes() -> None:
    from app.worker.rules.doc_rules_pure import CallUsage, Found, RuleUnchecked

    usage = CallUsage(0, 0, 0, (), 0, 0)
    human = _rule("R-h", "리뷰어 승인을 받는다.")
    found = Found(
        (RuleUnchecked(COMMITS, "pr", ""), RuleUnchecked(human, "human", "")), (), 0, usage
    )
    assert pr_check.pr_rules(found) == (COMMITS,)
    assert pr_check.pr_rules(None) == ()
