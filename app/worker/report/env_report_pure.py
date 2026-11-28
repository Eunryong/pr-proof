"""결과 코멘트의 환경 처방 부분 (DESIGN 4.9): 테스트 줄 아래에 펼쳐 두는 안내와 접는 블록 「이
저장소를 돌린 환경」.

펼쳐 두는 것은 사람이 결과를 다르게 읽어야 하는 것뿐이다: 처방을 확인할 때 base도 통과하지 않았음,
PR이 바꾼 의존성 입력을 쓰지 않았음. 접는 블록은 시작 이미지, 시스템과 의존성 명령, 저장소의 파일
대신 쓴 것, 빌드·테스트 단계, 층을 다시 썼는지, 처방을 확인할 때의 base 실행이다. 처방의 값과 모델이
쓴 글(명령, 경로, `why`, base가 빨갛다는 까닭)은 코드 스팬이나 울타리 안에만 둔다.
"""

from collections.abc import Mapping, Sequence
from typing import Final

from app.worker.core.markdown_pure import code_fence, inline_code
from app.worker.env.plan_pure import BaseRun
from app.worker.env.use_pure import EnvView, ignored_inputs_note
from app.worker.testing.test_run_pure import RunStatus

# 울타리 하나에 싣는 명령의 글자 수. GitHub 코멘트 하나의 상한(65,536자) 안에 규칙 절과 같이
# 들어가게, 울타리 넷(시스템, 의존성, 단계, base 실행)이 합쳐 그 4분의 1을 넘지 않게 잡았다.
# 처방 원문 전체는 env_plans에 있다
MAX_FENCE_CHARS: Final = 4000
MAX_ITEM_CHARS: Final = 300
SUMMARY: Final = "이 저장소를 돌린 환경"
LAYER_LABEL: Final[Mapping[str, str]] = {"system": "시스템 층", "deps": "의존성 층"}
_BASE_STATUS: Final[Mapping[RunStatus, str]] = {
    "passed": "통과",
    "failed": "테스트 실패",
    "build_failed": "빌드 실패",
    "no_tests": "실행된 테스트 없음",
    "timeout": "시간 초과",
    "cancelled": "중단",
    "unrunnable": "실행 불가",
    "no_env": "돌리지 않음",
}


def base_is_red(base: BaseRun) -> bool:
    """처방을 확인할 때 base도 통과하지 않았다(모델이 base가 빨갛다고 골랐다)."""
    return bool(base.base_is_red) or base.status not in ("passed", "no_tests")


def base_line(base: BaseRun) -> str:
    """처방을 확인할 때 base에서 돌린 결과 한 줄."""
    text = _BASE_STATUS[base.status]
    r = base.report
    if r is not None and r.total:
        text += f" ({r.total}개 중 통과 {r.passed}개)"
    failed = next((s for s in base.steps if s.exit_code != 0), None)
    if base.status in ("build_failed", "failed") and failed is not None:
        text += f", {inline_code(failed.name, 100)} 단계가 종료 코드 {failed.exit_code}로 끝남"
    if base.why:
        text += f": {inline_code(base.why, MAX_ITEM_CHARS)}"
    return text


def expanded(view: EnvView | None) -> list[str]:
    """테스트 줄 아래에 펼쳐 두는 줄들. 처방으로 돌리지 않았거나 알릴 것이 없으면 비었다."""
    if view is None:
        return []
    lines: list[str] = []
    if base_is_red(view.base_run):
        base = base_line(view.base_run)
        lines += ["", f"처방을 확인할 때 base 커밋도 통과하지 않았습니다 ({base}). "
                  "이 PR의 실패가 base에서 온 것일 수 있습니다."]  # fmt: skip
        if view.base_run.base_is_red:
            why = inline_code(view.base_run.base_is_red, MAX_ITEM_CHARS)
            lines.append(f"모델이 적은 base의 상태: {why}")
    note = ignored_inputs_note(view.use)
    if note is not None:
        lines += ["", note]
    return lines


def folded(view: EnvView | None) -> list[str]:
    """접는 블록 「이 저장소를 돌린 환경」. 처방으로 돌리지 않았으면 비었다."""
    if view is None:
        return []
    recipe, use = view.recipe, view.use
    lines = ["", f"<details><summary>{SUMMARY}</summary>", ""]
    lines.append(f"시작 이미지: {inline_code(recipe.system.image, MAX_ITEM_CHARS)}")
    lines += _fenced("시스템 명령 (root, 네트워크 열림):", recipe.system.commands)
    if recipe.system.fetches:
        lines += ["", "받은 저장소:"]
        lines += [
            f"- {inline_code(f.url, MAX_ITEM_CHARS)} 커밋 {inline_code(f.rev[:12], 20)} → "
            f"{inline_code(f.dest, MAX_ITEM_CHARS)}"
            for f in recipe.system.fetches
        ]
    if recipe.deps is not None:
        whose = "PR의 파일 (신뢰된 작성자)" if use.inputs == "head" else "base 커밋의 파일"
        paths = ", ".join(inline_code(p, MAX_ITEM_CHARS) for p in recipe.deps.inputs) or "없음"
        lines += ["", f"의존성 입력 ({whose}): {paths}"]
        lines += _fenced("의존성 명령 (네트워크 열림):", recipe.deps.commands)
    if recipe.overlays:
        lines += ["", "저장소의 파일 대신 쓴 것:"]
        lines += [
            f"- {inline_code(o.path, MAX_ITEM_CHARS)}: {inline_code(o.why, MAX_ITEM_CHARS)}"
            for o in recipe.overlays
        ]
    steps = [f"{s.name}: {s.command}" for s in recipe.build]
    if recipe.test is not None:
        steps.append(f"{recipe.test.name} (테스트): {recipe.test.command}")
    lines += _fenced("빌드·테스트 단계 (네트워크 없음):", steps)
    if use.layers:
        layers = " · ".join(
            f"{LAYER_LABEL.get(name, name)} {'다시 씀' if reused else '새로 구움'}"
            for name, reused in use.layers
        )
        lines += ["", f"층: {layers}"]
    lines += ["", f"처방을 확인할 때의 base 실행: {base_line(view.base_run)}"]
    note = ignored_inputs_note(use)
    if note is not None:
        lines += ["", note]
    return [*lines, "", "</details>"]


def _fenced(title: str, items: Sequence[str]) -> list[str]:
    if not items:
        return []
    text = "\n".join(items)
    if len(text) > MAX_FENCE_CHARS:
        text = text[:MAX_FENCE_CHARS] + "\n…"
    fence = code_fence(text)
    return ["", title, "", fence, text, fence]
