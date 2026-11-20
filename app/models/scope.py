"""수정안의 허용 변경 범위 판정 (docs/DESIGN.md F7 허용 변경 범위·범위 검사 방법·무시된 파일).

`git status --porcelain=v1 -z` 출력을 읽어 판정만 한다. git 실행은 `app/worker/scope.py`가 한다.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

# 범위 검사에서 빼는 파이프라인 산출물. `.venv`는 샌드박스의 .venv 마운트 지점이다
# (`app/sandbox/container.py`의 OUT_DIR, `app/sandbox/venv.py`의 VENV_IN_CONTAINER와 맞춘다)
PIPELINE_OUTPUTS: Final = (".pipeline-out/", ".venv/")
_PIPELINE_OUTPUT_DIRS: Final = (".pipeline-out", ".venv")

# 테스트 파일: 이 이름의 폴더 아래 모든 파일, 또는 이 패턴의 파일 이름
TEST_DIRS: Final = frozenset({"tests", "test"})
TEST_FILE_NAMES: Final = frozenset({"conftest.py"})
GITHUB_DIR: Final = ".github/"

# porcelain v1의 병합 충돌 상태 (XY)
_UNMERGED: Final = frozenset({"DD", "AU", "UD", "UA", "DU", "AA", "UU"})


@dataclass(frozen=True)
class StatusEntry:
    """`git status --porcelain=v1 -z` 한 줄. 이름 변경·복사면 orig_path가 있다."""

    code: str  # XY 두 글자. 예: " M", "??", "R ", "!!"
    path: str
    orig_path: str | None = None


def parse_porcelain(output: str) -> list[StatusEntry]:
    """`git status --porcelain=v1 -z` 출력. 이름 변경·복사는 다음 항목이 옛 경로다.

    -z 형식은 경로를 따옴표나 이스케이프 없이 그대로 내므로 공백·유니코드·줄바꿈이 든 경로도 읽는다.
    """
    parts = output.split("\0")
    entries: list[StatusEntry] = []
    i = 0
    while i < len(parts):
        item = parts[i]
        i += 1
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        orig = None
        if code[0] in "RC" or code[1] in "RC":
            orig = parts[i] if i < len(parts) else None
            i += 1
        entries.append(StatusEntry(code=code, path=path, orig_path=orig))
    return entries


def parse_ignored(output: str) -> frozenset[str]:
    """`--ignored` 출력에서 무시된 파일(`!!`)만. 파이프라인 산출물은 뺀다."""
    return frozenset(
        e.path for e in parse_porcelain(output) if e.code == "!!" and not is_pipeline_output(e.path)
    )


def is_pipeline_output(path: str) -> bool:
    return path.startswith(PIPELINE_OUTPUTS) or path in _PIPELINE_OUTPUT_DIRS


def is_test_file(path: str) -> bool:
    """테스트 파일인지. 경로만 보고 정한다 (리포 설정의 testpaths 등은 보지 않는다).

    `tests/`·`test/` 폴더 아래의 모든 파일(데이터·픽스처 포함, 어느 깊이든),
    `test_*.py`, `*_test.py`, `conftest.py`.
    """
    p = PurePosixPath(path.rstrip("/"))
    if any(part in TEST_DIRS for part in p.parts[:-1]):
        return True
    name = p.name
    return (
        name in TEST_FILE_NAMES
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


@dataclass(frozen=True)
class ScopePolicy:
    """수정 방식마다 다른 허용 규칙."""

    modify_only: bool  # 허용 파일의 수정(M)만 허락. 새 파일·삭제·이름 변경은 모두 밖이다
    protect_tests: bool  # 허용 목록에 있어도 테스트 파일 변경은 밖이다
    protect_github: bool  # 허용 목록에 있어도 `.github/` 변경은 밖이다
    # 테스트 파일이지만 바꿔도 되는 경로. 코드 이동에 따른 import 경로 변경용 (4단계 structure).
    # 호출하는 쪽이 바뀐 내용이 import 줄뿐인지 따로 확인해야 한다. 기본은 비어 있다
    test_files_allowed: frozenset[str] = frozenset()


# 기계적 수정: 위반이 있는 파일의 내용만 바꾼다. 파일을 만들거나 지울 이유가 없다.
# 테스트 파일의 위반도 고치므로 테스트 파일을 막지 않는다 (결과는 테스트 비교로 검증, 4.8의 1번)
MECHANICAL_POLICY: Final = ScopePolicy(modify_only=True, protect_tests=False, protect_github=False)
# 모델 수정: 허용 파일 안에서 새 파일·삭제·이름 변경까지 허락한다
AI_POLICY: Final = ScopePolicy(modify_only=False, protect_tests=True, protect_github=True)


@dataclass(frozen=True)
class ScopeViolation:
    code: str  # porcelain XY를 공백 없이. 예: "M", "??", "R", "!!"
    path: str
    reason: str

    @property
    def label(self) -> str:
        return f"{self.code} {self.path}"


@dataclass(frozen=True)
class ScopeResult:
    violations: tuple[ScopeViolation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def detail(self) -> str:
        """코멘트에 넣는 한 줄. 경로마다 이유를 붙인다."""
        return ", ".join(f"{v.label} ({v.reason})" for v in self.violations)


def _path_reason(path: str, allowed: set[str], policy: ScopePolicy) -> str | None:
    if policy.protect_github and path.startswith(GITHUB_DIR):
        return "`.github/` 변경"
    if policy.protect_tests and is_test_file(path) and path not in policy.test_files_allowed:
        return "테스트 파일 변경"
    if path not in allowed:
        return "허용 목록 밖"
    return None


def _entry_reason(e: StatusEntry, symlinks: frozenset[str]) -> str | None:
    """경로와 무관하게 버리는 변경."""
    if e.code in _UNMERGED:
        return "병합 충돌 상태"
    if "T" in e.code:
        return "파일 종류 변경"  # 일반 파일 ↔ 심볼릭 링크·서브모듈
    if e.path in symlinks:
        return "심볼릭 링크"
    return None


def judge_scope(
    entries: Iterable[StatusEntry],
    allowed: Iterable[str],
    policy: ScopePolicy,
    *,
    new_ignored: Iterable[str] = (),
    symlinks: frozenset[str] = frozenset(),
) -> ScopeResult:
    """허용 범위 밖의 변경을 모두 모은다. 하나라도 있으면 수정안 전체를 버린다.

    - 이름 변경·복사는 옛 경로와 새 경로가 둘 다 허용 범위 안이어야 한다.
    - symlinks: 바뀐 경로 중 지금 심볼릭 링크인 것 (worker가 확인해 넘긴다). 링크는 worktree 밖을
      가리킬 수 있어 허용 파일이라도 버린다.
    - new_ignored: AI 실행 직전 스냅샷에 없던 무시된 파일. 경로와 무관하게 버린다.
    - 경로가 모두 파이프라인 산출물인 변경은 보지 않는다.
    """
    allowed_set = set(allowed)
    bad: list[ScopeViolation] = []
    for e in entries:
        paths = [e.path] + ([e.orig_path] if e.orig_path else [])
        if all(is_pipeline_output(p) for p in paths):
            continue
        code = e.code.replace(" ", "")
        if policy.modify_only:
            if set(code) != {"M"}:
                bad.append(ScopeViolation(code, e.path, "수정 외 변경"))
                continue
        elif reason := _entry_reason(e, symlinks):
            bad.append(ScopeViolation(code, e.path, reason))
            continue
        for p in paths:
            if reason := _path_reason(p, allowed_set, policy):
                bad.append(ScopeViolation(code, p, reason))
    for p in sorted(new_ignored):
        if not is_pipeline_output(p):
            bad.append(ScopeViolation("!!", p, "새로 생긴 무시된 파일"))
    return ScopeResult(tuple(bad))
