"""수정안을 머지한 커밋을 다시 검사하지 않고 검증한 결과를 옮겨 적을지 정한다 (docs/DESIGN.md
4.9 "수정안 PR 상태 관리", 5.6).

원래 PR의 head가 파이프라인 수정안의 머지 커밋이고 그 내용이 검증한 수정안과 같으면, 규칙 검사와
테스트와 모델 호출 없이 앞 커밋의 검사 결과와 수정안 검증의 결과를 옮겨 적는다. 여기서는 판정과
옮겨 적을 값의 계산만 한다. 조건이 하나라도 맞지 않으면 그 까닭(글)을 돌려주고, 부르는 쪽은 전체
검사를 한다. 의심스러우면 전체 검사 쪽이다: 틀린 결과를 "검증됨"으로 남기는 것이 가장 나쁜 실패다.
"""

from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, replace
from typing import Final

from app.shared.db_pure import FixAttempt, Proposal, Run
from app.worker.diff_pure import line_after
from app.worker.doc_rules_pure import Found, RuleJudged
from app.worker.fix.fix_decision_pure import Decided
from app.worker.fix.fix_pure import MECHANICAL_BUNDLE, MechanicalFix
from app.worker.fix.model_fix_pure import MODEL_BUNDLE, ModelFix
from app.worker.test_run_pure import PytestRun

# 규칙 수정안의 묶음. 코멘트 수정안(feedback)은 규칙 검사의 결과를 고친 것이 아니라 넣지 않는다
RULE_BUNDLES: Final = frozenset({MECHANICAL_BUNDLE, MODEL_BUNDLE})
_SETTLED: Final = frozenset({"done", "error"})
NOT_MERGE_COMMIT: Final = (
    "수정안의 머지 커밋이 아님"  # 거의 모든 커밋의 까닭이라 로그에 남기지 않는다
)


@dataclass(frozen=True)
class CarriedFrom:
    """이 커밋의 결과를 옮겨 온 곳. 그래프 상태에 들어가고 결과 코멘트가 한 줄로 보인다."""

    number: int  # 머지한 수정안 PR
    bundle: str  # mechanical, model
    verified_sha: str  # 수정안을 검증한 원래 PR의 커밋


# 체크포인트에서 되살릴 수 있게 graph.py가 등록하는 타입
STATE_TYPES: Final = (("app.worker.merged_fix_pure", "CarriedFrom"),)


@dataclass(frozen=True)
class VerifiedCheck:
    """수정안을 검증한 커밋의 검사가 체크포인트에 남긴 값."""

    finished: bool  # 그래프가 멈춘 node 없이 코멘트까지 갔다
    base_sha: str | None  # 그 검사가 규칙과 설정을 읽은 base 커밋
    found: object  # doc_rules
    test_run: PytestRun | None
    decisions: Decided | None
    fix: MechanicalFix | ModelFix | None  # 머지한 묶음의 수정 결과
    pr_checks: object  # PR 단위 확인의 결과. PR 규칙이 없으면 None


def merged_proposal(proposals: Sequence[Proposal], head: str) -> Proposal | str:
    """head를 머지 커밋으로 가진 규칙 수정안. 없거나 옮겨 적을 수 없으면 까닭.

    같은 커밋에서 만든 다른 묶음의 수정안이 있으면 옮겨 적지 않는다. 하나만 머지됐으면 다른 묶음의
    수정안은 이번 작업이 닫고(superseded) 그 위반은 이 커밋에서 다시 고쳐야 하며, 둘 다 머지됐으면
    두 번째 머지 커밋의 부모가 검증한 커밋이 아니다.
    """
    merged = [p for p in proposals if p.state == "merged" and p.merge_commit == head]
    if not merged:
        return NOT_MERGE_COMMIT
    if len(merged) > 1:
        return "머지 커밋이 같은 수정안이 둘 이상"
    (p,) = merged
    if p.skill not in RULE_BUNDLES:
        return f"규칙 수정안이 아님 ({p.skill})"
    if p.attempt_id is None:
        return "수정안에 저장한 수정 시도가 없음"
    if any(q.id != p.id and q.head_sha == p.head_sha and q.skill in RULE_BUNDLES
           for q in proposals):  # fmt: skip
        return "검증한 커밋에 다른 묶음의 수정안이 있음"
    return p


def attempt_problem(attempt: FixAttempt | None, p: Proposal) -> str | None:
    """수정안이 올린 시도가 검증을 통과한 패치를 저장했고 그 수정안의 것인지."""
    if attempt is None or attempt.patch is None or attempt.file_hashes_json is None:
        return "검증을 통과한 패치를 저장한 기록이 없음"
    if (attempt.repo, attempt.pr_number, attempt.head_sha, attempt.bundle) != (
        p.repo, p.pr_number, p.head_sha, p.skill,
    ):  # fmt: skip
        return "저장한 수정 시도가 수정안과 맞지 않음"
    return None


def runs_problem(verified: Sequence[Run], head: Sequence[Run], pipeline_commit: str) -> str | None:
    """검사 기록으로 본 조건. verified는 검증한 커밋의 기록, head는 이 커밋의 기록이다.

    - 이 커밋에 끝난 검사가 이미 있으면 옮겨 적지 않는다. 그 작업은 동기화가 배포 뒤 옛 코드의
      결과를 다시 검사하려고 넣은 것(`new_pipeline`)이다. 옮겨 적기만 하면 옛 결과가 새 코드의
      기록으로 남는다.
    - 검증한 커밋의 끝난 검사가 모두 지금 코드의 것이어야 한다. 배포를 사이에 두면 옛 코드의
      결과를 지금 코드의 기록(`pipeline_commit`)으로 남기게 된다.
    """
    if any(r.state in _SETTLED for r in head):
        return "이 커밋의 끝난 검사가 이미 있음"
    done = [r for r in verified if r.state == "done"]
    if not done:
        return "검증한 커밋의 끝난 검사가 없음"
    if any((r.pipeline_commit or "") != pipeline_commit for r in done):
        return "검증한 커밋의 검사가 지금 파이프라인 코드의 것이 아님"
    return None


def state_problem(
    check: VerifiedCheck, *, bundle: str, attempt_id: int, base_sha: str | None
) -> str | None:
    """검증한 커밋의 체크포인트로 본 조건. 남은 일(승인 대기, 호출 상한으로 남은 판단, 다른 묶음의
    수정)이 있거나 PR 단위 규칙이 있으면 옮겨 적지 않는다. PR 단위 규칙은 PR의 커밋 목록을 보는데
    머지 커밋이 그 목록을 바꾼다."""
    if not check.finished:
        return "검증한 커밋의 검사가 끝까지 가지 않음"
    found, decisions, fix = check.found, check.decisions, check.fix
    if not isinstance(found, Found) or check.test_run is None or decisions is None:
        return "검증한 커밋의 규칙 결과, 테스트 결과, 수정 판단이 없음"
    if check.base_sha != base_sha:
        return "base가 그 뒤 바뀜"
    if fix is None or not fix.ready or fix.attempt_id != attempt_id:
        return "검증한 커밋의 수정 결과가 수정안의 시도와 다름"
    if isinstance(fix, ModelFix) and (fix.verification is None or fix.verification.unverified):
        return "도구로 재검사하지 못한 위반을 고친 수정안"
    if check.pr_checks is not None:
        return "PR 단위 규칙이 있음"
    if any(isinstance(o, RuleJudged) and o.left for o in found.outcomes):
        return "판단하지 못한 hunk가 남음"
    if any(d.reason == "call_limit" for d in decisions.decisions):
        return "호출 상한으로 정하지 못한 수정 판단이 남음"
    if any(d.action == "fix" and d.method != bundle for d in decisions.decisions):
        return "다른 묶음으로 고칠 위반이 있음"
    return None


def merge_problem(
    verified_sha: str, parents: Sequence[str], tree: str, expected: str
) -> str | None:
    """머지 커밋의 내용이 "검증한 커밋 + 저장한 패치"와 같은지. 부모에 검증한 커밋이 있어야 하고
    (머지 커밋, 스쿼시, 리베이스 모두), 트리가 같아야 한다. 검증한 커밋 뒤에 원래 브랜치에 커밋이
    더 올라온 뒤 머지했거나 머지에 다른 변경이 섞였으면 트리가 다르다."""
    if verified_sha not in parents:
        return "머지 커밋의 부모에 검증한 커밋이 없음"
    if tree != expected:
        return "머지 커밋의 내용이 검증한 수정안과 다름"
    return None


def fixed_ids(fix: MechanicalFix | ModelFix) -> frozenset[str]:
    """수정안이 고친 위반. 기계적 수정은 고친 뒤 없어진 것, 모델 수정은 출구 검사가 해소를 확인한
    대상이다(크기 상한으로 넣지 못한 위반은 대상이 아니다)."""
    return frozenset(fix.fixed if isinstance(fix, MechanicalFix) else fix.targets)


def carried(
    found: Found,
    decisions: Decided,
    fixed: Collection[str],
    patch: str,
    lines_of: Callable[[str], Sequence[str] | None],
) -> tuple[Found, Decided] | str:
    """수정안이 고친 위반을 빼고, 남은 위반(고치지 않기로 한 것 등)을 머지 커밋의 줄 번호로 옮긴다.

    lines_of(파일)은 머지 커밋의 그 파일 줄들이다(읽지 못하면 None). 패치가 남은 위반의 줄을
    바꿨거나, 옮긴 줄의 내용이 위반의 줄 내용과 다르거나, 파일 단위 위반의 파일이 없으면 까닭을
    돌려준다.
    """
    kept = []
    for v in found.violations:
        if v.id in fixed:
            continue
        lines = lines_of(v.file)
        if v.unit == "file" or v.line == 0:
            if lines is None:
                return f"남은 파일 위반의 파일을 머지 커밋에서 읽지 못함 ({v.file})"
            kept.append(v)
            continue
        line = line_after(patch, v.file, v.line)
        if line is None:
            return f"수정안이 남은 위반의 줄을 바꿈 ({v.file}:{v.line})"
        text = (v.line_text or "").strip()
        if (
            not text
            or lines is None
            or not 0 < line <= len(lines)
            or lines[line - 1].strip() != text
        ):
            return f"남은 위반의 줄을 머지 커밋에서 찾지 못함 ({v.file}:{v.line})"
        kept.append(replace(v, line=line))
    ids = {v.id for v in kept}
    return (
        replace(found, violations=tuple(kept)),
        replace(
            decisions, decisions=tuple(d for d in decisions.decisions if d.violation_id in ids)
        ),
    )
