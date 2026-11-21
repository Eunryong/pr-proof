"""수정안의 허용 변경 범위 판정 (docs/DESIGN.md F7 허용 변경 범위·범위 검사 방법·무시된 파일).

`git status --porcelain=v1 -z` 출력을 읽어 판정만 한다. git 실행은 `worker/fix/scope.py`가 한다.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

# 범위 검사에서 빼는 파이프라인 산출물. `.venv`는 샌드박스의 .venv 마운트 지점이다
# (`worker/sandbox/container.py`의 OUT_DIR, `worker/lang/python.py`의 VENV_IN_CONTAINER와 맞춘다)
PIPELINE_OUTPUTS: Final = (".pipeline-out/", ".venv/")
_PIPELINE_OUTPUT_DIRS: Final = (".pipeline-out", ".venv")

# 테스트 파일: 이 이름의 폴더 아래 모든 파일, 또는 이 패턴의 파일 이름
TEST_DIRS: Final = frozenset({"tests", "test"})
TEST_FILE_NAMES: Final = frozenset({"conftest.py"})
GITHUB_DIR: Final = ".github/"
# 수정 작성의 모델이 새로 만드는 테스트 파일 (4.7). 저장소의 테스트 폴더 바로 아래, 이름으로
# 파이프라인이 만든 것임이 보이는 새 파일만이다. 있던 테스트 파일, conftest.py, 테스트 설정은 밖이다
GENERATED_TEST_PREFIX: Final = "test_pipeline_"
_GENERATED_NAME: Final = re.compile(r"test_pipeline_[a-z0-9_]{1,40}\.py")
# 새 파일로 보이는 porcelain 상태: 추적하지 않는 새 파일, 그리고 diff에 보이려고 `git add -N`한 파일
_NEW_FILE_CODES: Final = frozenset({"??", " A"})

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


def generated_test_dir(dirs: Iterable[str]) -> str:
    """새 테스트 파일을 둘 폴더. dirs는 저장소 맨 위의 진짜 폴더 이름들이다. `tests`가 있으면
    그것, 없고 `test`가 있으면 그것, 둘 다 없으면 `tests`."""
    names = set(dirs)
    return "test" if "tests" not in names and "test" in names else "tests"


def is_generated_test(path: str, test_dir: str) -> bool:
    """test_dir 바로 아래의 `test_pipeline_<이름>.py`인지. 이름은 영문 소문자·숫자·밑줄 40자까지."""
    folder, _, name = path.rpartition("/")
    return folder == test_dir and bool(_GENERATED_NAME.fullmatch(name))


@dataclass(frozen=True)
class ScopePolicy:
    """수정 방식마다 다른 허용 규칙."""

    modify_only: bool  # 허용 파일의 수정(M)만 허락. 새 파일·삭제·이름 변경은 모두 밖이다
    protect_tests: bool  # 허용 목록에 있어도 테스트 파일 변경은 밖이다
    protect_github: bool  # 허용 목록에 있어도 `.github/` 변경은 밖이다
    # 테스트 파일이지만 바꿔도 되는 경로. 코드 이동에 따른 import 경로 변경용 (4단계 structure).
    # 호출하는 쪽이 바뀐 내용이 import 줄뿐인지 따로 확인해야 한다. 기본은 비어 있다
    test_files_allowed: frozenset[str] = frozenset()
    # 새로 만들어도 되는 테스트 파일의 폴더 (`is_generated_test`). None이면 만들 수 없다
    generated_test_dir: str | None = None


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


def _new_generated_test(e: StatusEntry, policy: ScopePolicy) -> bool:
    """정한 자리에 새로 만든 테스트 파일. 이름 변경·복사의 결과는 새 파일로 보지 않는다."""
    return (
        policy.generated_test_dir is not None
        and e.orig_path is None
        and e.code in _NEW_FILE_CODES
        and is_generated_test(e.path, policy.generated_test_dir)
    )


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
    - policy.generated_test_dir가 있으면 그 폴더에 정한 이름으로 새로 만든 테스트 파일은 허용 목록
      밖이어도 받는다. 같은 이름이라도 있던 파일의 수정·삭제는 테스트 파일 변경이다.
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
        if _new_generated_test(e, policy):
            continue
        for p in paths:
            if reason := _path_reason(p, allowed_set, policy):
                bad.append(ScopeViolation(code, p, reason))
    for p in sorted(new_ignored):
        if not is_pipeline_output(p):
            bad.append(ScopeViolation("!!", p, "새로 생긴 무시된 파일"))
    return ScopeResult(tuple(bad))
