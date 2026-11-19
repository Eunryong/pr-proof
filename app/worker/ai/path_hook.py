"""claude CLI의 PreToolUse 훅: 도구가 가리키는 경로의 실제 위치가 작업 폴더 밖이면 거부한다.

파이프라인이 `python -I path_hook.py <작업 폴더> <기록 파일>`로 등록한다 (docs/DESIGN.md 8.4).
표준 입력으로 도구 호출 한 건의 JSON을 받는다. 판단에는 심볼릭 링크를 모두 푼 경로를 쓴다.
모르는 도구, 경로가 없는 호출, 읽지 못한 입력은 모두 거부한다. 이 파일은 CLI가 따로 실행하므로
표준 라이브러리만 쓴다.

기록 파일에는 호출마다 한 줄을 남긴다. 파이프라인은 이 줄 수로 훅이 실제로 돌았는지 확인한다.
"""

import json
import os
import sys
from collections.abc import Mapping
from typing import Final

# 도구 → 경로가 든 입력 키. 값이 True면 꼭 있어야 한다. 없어도 되는 키는 없으면 작업 폴더로 본다
_PATH_KEYS: Final[Mapping[str, Mapping[str, bool]]] = {
    "Read": {"file_path": True},
    "Write": {"file_path": True},
    "Edit": {"file_path": True},
    "Grep": {"path": False},
    "Glob": {"path": False},
}
# 경로가 아니라 패턴인 입력. 패턴이 폴더를 벗어나는 모양이면 거부한다
_PATTERN_KEYS: Final[Mapping[str, tuple[str, ...]]] = {"Grep": ("glob",), "Glob": ("pattern",)}
_WILDCARDS: Final = "*?[{"


def inside(root: str, path: str) -> bool:
    """path의 실제 위치가 root(이미 실제 경로) 안인지. 상대 경로는 root 기준으로 읽는다."""
    real = os.path.realpath(os.path.join(root, path))
    return real == root or real.startswith(root + os.sep)


def pattern_escapes(root: str, pattern: str) -> bool:
    """glob 패턴이 root 밖을 볼 수 있는 모양인지. `..` 조각이 있거나, 와일드카드 앞부분이 밖이다."""
    parts = pattern.replace("\\", "/").split("/")
    if ".." in parts:
        return True
    literal: list[str] = []
    for part in parts:
        if any(c in part for c in _WILDCARDS):
            break
        literal.append(part)
    prefix = "/".join(literal)
    return bool(prefix) and not inside(root, prefix)


def problem(root: str, event: object) -> str | None:
    """이 도구 호출을 거부할 이유. 허용하면 None."""
    if not isinstance(event, dict):
        return "입력이 JSON 객체가 아님"
    tool, tool_input = event.get("tool_name"), event.get("tool_input")
    if tool not in _PATH_KEYS:
        return f"허용 목록에 없는 도구: {tool!r}"
    if not isinstance(tool_input, dict):
        return "도구 입력이 없음"
    for key, required in _PATH_KEYS[tool].items():
        value = tool_input.get(key)
        if value is None and not required:
            continue
        if not isinstance(value, str) or not value or "\0" in value:
            return f"{key}가 없거나 문자열이 아님"
        if not inside(root, value):
            return f"작업 폴더 밖의 경로: {key}"
    for key in _PATTERN_KEYS.get(tool, ()):
        value = tool_input.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or "\0" in value or pattern_escapes(root, value):
            return f"작업 폴더 밖을 보는 패턴: {key}"
    return None


def main(argv: list[str]) -> int:
    try:
        root, log_path = os.path.realpath(argv[1]), argv[2]
        event = json.loads(sys.stdin.read())
        reason = problem(root, event)
        tool = event.get("tool_name") if isinstance(event, dict) else None
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(json.dumps({"tool": tool, "denied": reason}, ensure_ascii=False) + "\n")
    except Exception as e:  # 판단하지 못하면 막는다. 종료 코드 2는 CLI가 도구 호출을 막는 오류다
        print(f"경로 감시 훅 실패: {type(e).__name__}", file=sys.stderr)
        return 2
    if reason is not None:
        decision = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
        print(json.dumps({"hookSpecificOutput": decision}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
