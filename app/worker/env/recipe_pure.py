"""환경 처방: 저장소를 빌드·테스트할 환경의 데이터, 경계의 검사, 층의 키.

처방은 믿지 않는 JSON으로 받는다(다음 단계에서는 모델이 쓴다). `validate`가 경계에서 한 번 검사해
`EnvRecipe`로 바꾸고, 그 뒤의 코드는 다시 검사하지 않는다. 처방에는 네트워크, 마운트, UID, 자원과
시간 상한의 칸이 없다. 그것은 프로그램이 정한다(`env/install.py`의 `Limits`).

층은 둘이다. 시스템 층은 시작 이미지 위에서 root로 `system.commands`와 `system.fetches`를 돌린다.
의존성 층은 그 위에서 호스트 UID로 `deps.inputs`(저장소의 파일)를 `/opt/env/src/`에, `overlays`를
`/opt/env/overlay/`에 넣고 `deps.commands`를 돌린다. `overlays`는 저장소에 쓰지 않는다.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Final, Literal

TestReportKind = Literal["junit", "exit"]
_REPORT_KINDS: Final = ("junit", "exit")

ENV_ROOT: Final = "/opt/env"  # 층의 파일이 놓이는 곳. 프로그램이 만들고 호스트 UID에게 준다
SRC_DIR: Final = f"{ENV_ROOT}/src"  # deps.inputs를 복사해 넣는 곳. deps.commands의 작업 폴더
OVERLAY_DIR: Final = f"{ENV_ROOT}/overlay"  # overlays를 쓰는 곳
OUT_DIR: Final = ".pipeline-out"  # 실행 단계가 결과 파일을 쓰는 저장소 안의 폴더
# 프로그램이 쓰는 /opt/env 아래 이름. fetches의 dest로 쓰지 못한다
_RESERVED_DEST: Final = ("src", "overlay")
# 프로그램이 층을 굽는 방법(준비 명령, 받기, 복사)을 바꾸면 올린다. 층의 키에 들어간다
BAKE_VERSION: Final = "1"

# 경계의 상한. 처방은 사람이 읽고 결과 코멘트에 보일 크기여야 하고, 덮어쓰기 파일은 포트 파일 같은
# 작은 글이다. DemoCue 처방(명령 10개 안, 덮어쓰기 2개 4KB)의 열 배 이상으로 잡았다
MAX_COMMANDS: Final = 50
MAX_COMMAND_CHARS: Final = 8000
MAX_FETCHES: Final = 10
MAX_INPUTS: Final = 200
MAX_OVERLAYS: Final = 50
MAX_OVERLAY_BYTES: Final = 256 * 1024
MAX_OVERLAYS_TOTAL_BYTES: Final = 1024 * 1024
MAX_WHY_CHARS: Final = 1000
MAX_ENV: Final = 50
MAX_ENV_VALUE_CHARS: Final = 4000
MAX_STEPS: Final = 20
MAX_OUTPUTS: Final = 20
MAX_PATH_CHARS: Final = 255
MAX_IMAGE_CHARS: Final = 255
MAX_URL_CHARS: Final = 2000

_COMPONENT: Final = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_IMAGE: Final = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?/)?"
    rf"{_COMPONENT}(?:/{_COMPONENT})*"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?"
)
# 사용자 정보(@), 질의(?), 따옴표, 공백이 없는 https 주소
_URL: Final = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%/+-]*)?")
_REV: Final = re.compile(r"[0-9a-f]{40}")
_ENV_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_STEP_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# 결과 파일은 .pipeline-out/ 바로 아래의 파일 하나다. 하위 폴더를 두지 않아 읽을 때 중간 경로의
# 링크를 볼 일이 없다
_REPORT_PATH: Final = re.compile(rf"{re.escape(OUT_DIR)}/[A-Za-z0-9_][A-Za-z0-9._-]{{0,63}}")


@dataclass(frozen=True)
class GitFetch:
    """시스템 층에서 받는 git 저장소. rev(40자리 커밋)가 내용의 고정이다. dest는 /opt/env 아래."""

    url: str
    rev: str
    dest: str


@dataclass(frozen=True)
class SystemLayer:
    image: str
    commands: tuple[str, ...]
    fetches: tuple[GitFetch, ...]


@dataclass(frozen=True)
class DepsLayer:
    inputs: tuple[str, ...]  # 저장소 기준 경로. /opt/env/src/<경로>로 복사한다
    commands: tuple[str, ...]


@dataclass(frozen=True)
class Overlay:
    """저장소 파일 대신 샌드박스 안에서만 쓰는 파일. /opt/env/overlay/<path>에 쓴다."""

    path: str
    content: str
    why: str


@dataclass(frozen=True)
class Step:
    name: str
    command: str


@dataclass(frozen=True)
class TestStep:
    __test__ = False  # 이름이 Test로 시작해도 pytest가 테스트 클래스로 모으지 않게

    name: str
    command: str
    report: TestReportKind
    report_path: str  # report가 junit이면 .pipeline-out/<이름>, exit이면 ""


@dataclass(frozen=True)
class EnvRecipe:
    system: SystemLayer
    deps: DepsLayer | None
    overlays: tuple[Overlay, ...]
    env: tuple[tuple[str, str], ...]  # 빌드·테스트 단계의 환경 변수
    build: tuple[Step, ...]
    test: TestStep | None
    outputs: tuple[str, ...]  # 빌드 산출물 폴더 (저장소 기준)


def validate(data: Mapping[str, object]) -> EnvRecipe | list[str]:
    """처방 JSON을 검사해 EnvRecipe로 바꾼다. 틀리면 고칠 곳을 한 줄씩 적은 목록이다."""
    p = _Problems()
    if not isinstance(data, Mapping):
        return ["처방: JSON 객체여야 함"]
    p.unknown_keys(data, "처방", ("system", "deps", "overlays", "env", "build", "test", "outputs"))
    system = _system(data.get("system"), p)
    deps = None if data.get("deps") is None else _deps(data["deps"], p)
    overlays = _overlays(data.get("overlays", []), p)
    if overlays and data.get("deps") is None:
        p.add("overlays", "의존성 층에 쓰므로 deps가 있어야 함 (명령이 없으면 commands를 비운다)")
    env = _env(data.get("env", {}), p)
    build = _build(data.get("build", []), p)
    test = None if data.get("test") is None else _test(data["test"], p)
    outputs = tuple(
        out.rstrip("/")
        for out in p.strings(data.get("outputs", []), "outputs", MAX_OUTPUTS, MAX_PATH_CHARS)
        if p.path(out.rstrip("/"), "outputs")
    )
    names = [s.name for s in build] + ([test.name] if test is not None else [])
    for name in sorted({n for n in names if names.count(n) > 1}):
        p.add("build/test", f"단계 이름 {name!r}이 겹침")
    if p.items or system is None:
        return p.items or ["system: 있어야 함"]
    return EnvRecipe(system, deps, overlays, env, build, test, outputs)


def recipe_json(recipe: EnvRecipe) -> dict[str, object]:
    """처방을 JSON 값으로 (validate가 받는 모양과 같다)."""
    value = {
        "system": asdict(recipe.system),
        "deps": None if recipe.deps is None else asdict(recipe.deps),
        "overlays": [asdict(o) for o in recipe.overlays],
        "env": dict(recipe.env),
        "build": [asdict(s) for s in recipe.build],
        "test": None if recipe.test is None else asdict(recipe.test),
        "outputs": list(recipe.outputs),
    }
    return json.loads(json.dumps(value))  # 튜플을 배열로


def recipe_key(recipe: EnvRecipe) -> str:
    return _hash(recipe_json(recipe))


def system_key(recipe: EnvRecipe, image_digest: str, user: str) -> str:
    """시스템 층의 키: 시작 이미지 digest, 층의 명세, 호스트 UID(/opt/env의 주인), 굽는 방법."""
    return _hash(
        {"bake": BAKE_VERSION, "layer": "system", "parent": image_digest, "user": user,
         "spec": asdict(recipe.system)}
    )  # fmt: skip


def deps_key(recipe: EnvRecipe, parent: str, user: str, inputs: Mapping[str, bytes]) -> str:
    """의존성 층의 키: 시스템 층 키, 층의 명세, 호스트 UID, 복사해 넣는 파일과 덮어쓰기 파일의 내용.

    덮어쓰기 파일의 why는 설명이라 넣지 않는다.
    """
    return _hash(
        {"bake": BAKE_VERSION, "layer": "deps", "parent": parent, "user": user,
         "spec": None if recipe.deps is None else asdict(recipe.deps),
         "overlays": [[o.path, o.content] for o in recipe.overlays],
         "inputs": {path: hashlib.sha256(data).hexdigest() for path, data in inputs.items()}}
    )  # fmt: skip


def _hash(value: object) -> str:
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


class _Problems:
    """고칠 곳을 모은다. 각 줄은 `칸: 까닭`이다."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, where: str, why: str) -> None:
        self.items.append(f"{where}: {why}")

    def obj(self, value: object, where: str, keys: tuple[str, ...]) -> Mapping[str, object] | None:
        if not isinstance(value, Mapping):
            self.add(where, "JSON 객체여야 함")
            return None
        self.unknown_keys(value, where, keys)
        return value

    def unknown_keys(self, value: Mapping[str, object], where: str, keys: tuple[str, ...]) -> None:
        for key in value:
            if key not in keys:
                self.add(where, f"모르는 칸 {key!r} (쓸 수 있는 칸: {', '.join(keys)})")

    def text(self, value: object, where: str, max_chars: int, *, empty: bool = False) -> str | None:
        if not isinstance(value, str):
            self.add(where, "문자열이어야 함")
            return None
        if not value and not empty:
            self.add(where, "비어 있음")
            return None
        if len(value) > max_chars:
            self.add(where, f"{max_chars}자를 넘음 ({len(value)}자)")
            return None
        if "\0" in value:
            self.add(where, "NUL 문자가 있음")
            return None
        return value

    def items_of(self, value: object, where: str, max_items: int) -> list[object]:
        if not isinstance(value, list):
            self.add(where, "배열이어야 함")
            return []
        if len(value) > max_items:
            self.add(where, f"{max_items}개를 넘음 ({len(value)}개)")
            return []
        return value

    def strings(self, value: object, where: str, max_items: int, max_chars: int) -> tuple[str, ...]:
        found = (
            self.text(item, f"{where}[{i}]", max_chars)
            for i, item in enumerate(self.items_of(value, where, max_items))
        )
        return tuple(s for s in found if s is not None)

    def path(self, value: str, where: str) -> bool:
        """상대 경로이고 `..`·`.`·빈 조각이 없고 `-`나 `/`로 시작하지 않는지."""
        why = path_problem(value)
        if why is not None:
            self.add(where, f"{why} ({value!r})")
        return why is None


def path_problem(path: str) -> str | None:
    """저장소 기준 상대 경로로 쓸 수 없으면 그 까닭."""
    if not path:
        return "경로가 비어 있음"
    if len(path) > MAX_PATH_CHARS:
        return f"경로가 {MAX_PATH_CHARS}자를 넘음"
    if path.startswith("/"):
        return "절대 경로는 쓸 수 없음"
    if path.startswith("-"):
        return "'-'로 시작하는 경로는 쓸 수 없음"
    if "\\" in path or "\0" in path:
        return "경로에 '\\'나 NUL이 있음"
    if any(part in ("", ".", "..") for part in path.split("/")):
        return "경로에 빈 조각이나 '.', '..'이 있음"
    return None


def _system(value: object, p: _Problems) -> SystemLayer | None:
    obj = p.obj(value, "system", ("image", "commands", "fetches"))
    if obj is None:
        return None
    image = p.text(obj.get("image"), "system.image", MAX_IMAGE_CHARS)
    if image is not None and not _IMAGE.fullmatch(image):
        p.add("system.image", f"이미지 참조 모양이 아님 ({image!r}, 예: debian:bookworm-slim)")
    commands = p.strings(
        obj.get("commands", []), "system.commands", MAX_COMMANDS, MAX_COMMAND_CHARS
    )
    fetches = tuple(
        f
        for i, item in enumerate(p.items_of(obj.get("fetches", []), "system.fetches", MAX_FETCHES))
        if (f := _fetch(item, f"system.fetches[{i}]", p)) is not None
    )
    dests = [f.dest for f in fetches]
    for dest in sorted({d for d in dests if dests.count(d) > 1}):
        p.add("system.fetches", f"dest {dest!r}가 겹침")
    return SystemLayer(image or "", commands, fetches)


def _fetch(value: object, where: str, p: _Problems) -> GitFetch | None:
    obj = p.obj(value, where, ("url", "rev", "dest"))
    if obj is None:
        return None
    url = p.text(obj.get("url"), f"{where}.url", MAX_URL_CHARS)
    rev = p.text(obj.get("rev"), f"{where}.rev", 40)
    dest = p.text(obj.get("dest"), f"{where}.dest", MAX_PATH_CHARS)
    if url is not None and not _URL.fullmatch(url):
        p.add(f"{where}.url", f"https 주소여야 함 (사용자 정보·질의 없이) ({url!r})")
        url = None
    if rev is not None and not _REV.fullmatch(rev):
        p.add(f"{where}.rev", f"40자리 16진수 커밋이어야 함 ({rev!r})")
        rev = None
    if dest is not None and not p.path(dest, f"{where}.dest"):
        dest = None
    elif dest is not None and dest.split("/")[0] in _RESERVED_DEST:
        p.add(f"{where}.dest", f"{ENV_ROOT}/{dest.split('/')[0]}은 프로그램이 쓰는 자리임")
        dest = None
    if url is None or rev is None or dest is None:
        return None
    return GitFetch(url, rev, dest)


def _deps(value: object, p: _Problems) -> DepsLayer | None:
    obj = p.obj(value, "deps", ("inputs", "commands"))
    if obj is None:
        return None
    inputs = tuple(
        path
        for path in p.strings(obj.get("inputs", []), "deps.inputs", MAX_INPUTS, MAX_PATH_CHARS)
        if p.path(path, "deps.inputs")
    )
    for dup in sorted({i for i in inputs if inputs.count(i) > 1}):
        p.add("deps.inputs", f"{dup!r}가 겹침")
    commands = p.strings(obj.get("commands", []), "deps.commands", MAX_COMMANDS, MAX_COMMAND_CHARS)
    return DepsLayer(inputs, commands)


def _overlays(value: object, p: _Problems) -> tuple[Overlay, ...]:
    overlays = []
    for i, item in enumerate(p.items_of(value, "overlays", MAX_OVERLAYS)):
        where = f"overlays[{i}]"
        obj = p.obj(item, where, ("path", "content", "why"))
        if obj is None:
            continue
        path = p.text(obj.get("path"), f"{where}.path", MAX_PATH_CHARS)
        content = p.text(obj.get("content"), f"{where}.content", MAX_OVERLAY_BYTES, empty=True)
        why = p.text(obj.get("why"), f"{where}.why", MAX_WHY_CHARS)
        if content is not None and len(content.encode()) > MAX_OVERLAY_BYTES:
            p.add(f"{where}.content", f"{MAX_OVERLAY_BYTES}바이트를 넘음")
            content = None
        if path is not None and not p.path(path, f"{where}.path"):
            path = None
        if path is not None and content is not None and why is not None:
            overlays.append(Overlay(path, content, why))
    paths = [o.path for o in overlays]
    for dup in sorted({x for x in paths if paths.count(x) > 1}):
        p.add("overlays", f"path {dup!r}가 겹침")
    total = sum(len(o.content.encode()) for o in overlays)
    if total > MAX_OVERLAYS_TOTAL_BYTES:
        p.add("overlays", f"내용의 합이 {MAX_OVERLAYS_TOTAL_BYTES}바이트를 넘음 ({total}바이트)")
    return tuple(overlays)


def _env(value: object, p: _Problems) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, Mapping):
        p.add("env", 'JSON 객체여야 함 ({"이름": "값"})')
        return ()
    if len(value) > MAX_ENV:
        p.add("env", f"{MAX_ENV}개를 넘음 ({len(value)}개)")
        return ()
    pairs = []
    for name, raw in value.items():
        if not _ENV_NAME.fullmatch(name):
            p.add("env", f"환경 변수 이름 모양이 아님 ({name!r})")
            continue
        text = p.text(raw, f"env.{name}", MAX_ENV_VALUE_CHARS, empty=True)
        if text is not None:
            pairs.append((name, text))
    return tuple(pairs)


def _step_fields(obj: Mapping[str, object], where: str, p: _Problems) -> tuple[str, str] | None:
    name = p.text(obj.get("name"), f"{where}.name", 32)
    if name is not None and not _STEP_NAME.fullmatch(name):
        p.add(f"{where}.name", f"소문자·숫자·'-'·'_'로 32자 안이어야 함 ({name!r})")
        name = None
    command = p.text(obj.get("command"), f"{where}.command", MAX_COMMAND_CHARS)
    return (name, command) if name is not None and command is not None else None


def _build(value: object, p: _Problems) -> tuple[Step, ...]:
    steps = []
    for i, item in enumerate(p.items_of(value, "build", MAX_STEPS)):
        obj = p.obj(item, f"build[{i}]", ("name", "command"))
        fields = None if obj is None else _step_fields(obj, f"build[{i}]", p)
        if fields is not None:
            steps.append(Step(*fields))
    return tuple(steps)


def _test(value: object, p: _Problems) -> TestStep | None:
    obj = p.obj(value, "test", ("name", "command", "report", "report_path"))
    if obj is None:
        return None
    fields = _step_fields(obj, "test", p)
    raw_report = obj.get("report")
    report = next((kind for kind in _REPORT_KINDS if kind == raw_report), None)
    if report is None:
        p.add("test.report", f"{' 또는 '.join(_REPORT_KINDS)}이어야 함 ({raw_report!r})")
        return None
    report_path = obj.get("report_path", "")
    if report == "junit":
        if not isinstance(report_path, str) or not _REPORT_PATH.fullmatch(report_path):
            p.add(
                "test.report_path",
                f"junit이면 {OUT_DIR}/ 바로 아래의 파일 이름이어야 함 ({report_path!r}, "
                f"예: {OUT_DIR}/junit.xml)",
            )
            return None
    elif report_path != "":
        p.add("test.report_path", "report가 exit이면 비워 둔다")
        return None
    if fields is None or not isinstance(report_path, str):
        return None
    return TestStep(fields[0], fields[1], report, report_path)
