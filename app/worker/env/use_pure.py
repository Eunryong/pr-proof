"""PR 흐름에서 환경 처방을 쓸 때의 결정: 상태에 두는 처방의 참조, 의존성 입력과 작성자 신뢰, 처방이
없거나 층을 굽지 못했을 때 보이는 까닭 (DESIGN 4.5 "Python 프로필이 돌릴 것이 없는 저장소").

까닭의 글은 프로그램이 쓴다. 그 안에 넣는 모델의 글(돌릴 것이 없다는 까닭, 호출 실패의 출력)과
처방의 값(단계 이름, 경로)은 코드 스팬 안에만 둔다.
"""

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Final, assert_never

from app.worker.core.markdown_pure import inline_code
from app.worker.env.install_pure import BakeFailed
from app.worker.env.plan_pure import BaseRun, UnplannedKind
from app.worker.env.recipe_pure import EnvRecipe
from app.worker.testing.test_run_pure import EnvUse, InputsSource, StepRun, TestRun

MAX_REASON_CHARS: Final = 300
MAX_PATH_CHARS: Final = 200

# 체크포인트에서 되살릴 수 있게 flow_state.py가 등록하는 타입
STATE_TYPES: Final = (
    ("app.worker.env.use_pure", "EnvPlanned"),
    ("app.worker.env.use_pure", "EnvSkipped"),
)

# 환경 준비(`plan_env`)를 거치지 않은 작업이 처방이 필요한 저장소의 테스트를 불렀다
NOT_PREPARED: Final = "이 작업은 환경 처방을 정하지 않아 이 저장소를 돌리지 않음"


@dataclass(frozen=True)
class EnvPlanned:
    """base에서 확인한 처방의 id. 처방 본문은 env_plans에서 다시 읽는다."""

    plan_id: int


@dataclass(frozen=True)
class EnvSkipped:
    """처방이 없다. reason은 테스트 결과의 까닭(`TestRun.unplanned`)에 그대로 쓰는 글이다."""

    reason: str


EnvRef = EnvPlanned | EnvSkipped


@dataclass(frozen=True)
class InputsChoice:
    """의존성 층을 구울 입력의 커밋과, PR이 바꾼 deps.inputs."""

    source: InputsSource
    changed: tuple[str, ...]


@dataclass(frozen=True)
class EnvView:
    """결과 코멘트의 환경 블록이 그리는 값: 저장한 처방, 처방을 확인할 때의 base 실행, 이번 실행."""

    recipe: EnvRecipe
    base_run: BaseRun
    use: EnvUse


def unplanned_reason(kind: UnplannedKind, reason: str, *, rounds: int, last_round: str) -> str:
    """처방 정하기가 처방을 내지 못한 까닭. reason은 `EnvUnplanned.reason`, last_round는 마지막
    회차의 요약(프로그램이 쓴 글이지만 단계 이름이 들어 있다)이다."""
    match kind:
        case "nothing_to_run":
            return "모델이 base 커밋을 읽고 돌릴 빌드·테스트가 없다고 답함: " + inline_code(
                reason, MAX_REASON_CHARS
            )
        case "unverified":
            last = inline_code(last_round, MAX_REASON_CHARS)
            last = f" (마지막 회차: {last})" if last_round else ""
            return f"모델이 쓴 환경 처방이 base 커밋에서 {rounds}회 안에 통과하지 못함{last}"
        case "cap":
            return f"환경 처방을 정하는 모델 호출이 상한에 걸림 ({reason})"
        case "model_failed":
            return "환경 처방을 정하는 모델 호출이 실패함: " + inline_code(reason, MAX_REASON_CHARS)
        case _:
            assert_never(kind)


def unreadable_reason(plan_id: int) -> str:
    return f"저장한 환경 처방 #{plan_id}을 지금의 처방 검사로 읽지 못함"


def choose_inputs(
    changed: Collection[str], inputs: Sequence[str], *, trusted: bool
) -> InputsChoice:
    """의존성 층을 구울 입력. 네트워크가 열린 곳에서 도는 입력은 base나 믿는 작성자가 고른 것뿐이다
    (위협 T40). PR이 deps.inputs 중 하나라도 바꿨고 작성자를 믿으면 head, 아니면 base다."""
    touched = tuple(path for path in inputs if path in changed)
    return InputsChoice("head" if touched and trusted else "base", touched)


def ignored_inputs_note(use: EnvUse) -> str | None:
    """PR이 바꿨지만 쓰지 않은 의존성 입력의 안내. 없으면 None."""
    if use.inputs == "head" or not use.changed_inputs:
        return None
    paths = ", ".join(inline_code(p, MAX_PATH_CHARS) for p in use.changed_inputs)
    return f"PR이 바꾼 {paths}는 쓰지 않음 (신뢰된 작성자가 아님)"


def bake_failed_run(failed: BakeFailed, use: EnvUse) -> TestRun:
    """층을 굽지 못한 실행. 실행 불가로 보이고, 굽던 단계와 출력의 끝을 테스트 단계 자리에 둔다.

    PR의 입력으로 굽다 실패했으면 PR이 바꾼 입력 탓이다. base의 입력으로 실패했으면 base에서 확인한
    처방을 지금 다시 굽지 못한 것이다(받는 곳이 바뀜 등).
    """
    if failed.timed_out:
        what = "시간 상한을 넘음"
    elif failed.exit_code is None:
        what = "입력을 읽지 못함"
    else:
        what = f"종료 코드 {failed.exit_code}"
    if use.inputs == "head":
        source = "PR이 바꾼 의존성 입력으로 환경을 준비하지 못함"
    else:
        source = "base에서 확인한 환경을 다시 준비하지 못함"
    why = f"{source} ({inline_code(failed.step, MAX_PATH_CHARS)}, {what})"
    step = StepRun(failed.step, failed.exit_code, output=failed.output_tail)
    return TestRun(test=step, verdict="unrunnable", unrunnable_why=why, env=use)
