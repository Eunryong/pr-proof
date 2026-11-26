"""configure·add·rm·list의 판정과 글 (외부 호출 없음).

- repo 이름 확인
- config.yaml 고치기: `repos`에 더하고 빼고, `model.default`와 `rag.allowed`를 적고 지우되 다른
  설정과 주석을 지킨다 (글을 줄 단위로 고치고, 고친 뒤 YAML로 읽어 바뀐 것이 그 키뿐인지 확인한다)
- 모델 이름 확인과 단계별로 쓰일 모델의 글
- 과거 수정 기록 검색(`rag`)의 상태 글과 받을 크기
- 웹훅: 만들지 고칠지, 이벤트, 손으로 등록할 값
- 빠진 프로그램 안내, 끝 확인 표, 토큰 권한 안내
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import yaml

from app.shared.comment_pure import read_owner
from app.shared.embed_models_pure import EmbedModel
from app.shared.model_names_pure import MODEL_NAME_PATTERN
from app.worker.config.repo_config_pure import (
    STAGE_MODELS,
    ModelSettings,
    RepoConfig,
    with_default_model,
)

REPO_EXAMPLE: Final = "octocat/hello-world"
MODEL_EXAMPLES: Final = "sonnet, opus, haiku, claude-sonnet-4-5"
DEFAULT_MODEL_WORD: Final = "default"  # `model default`: 파이프라인 쪽 값을 지운다
STAGE_LABELS: Final[Mapping[str, str]] = {
    "rules": "규칙 읽기",
    "checker": "검사기 작성",
    "judge": "위반 판단, PR 설명·이슈",
    "decide": "수정 판단",
    "light": "수정 작성",
    "heavy": "수정 작성 (올린 뒤)",  # 가벼운 모델이 이어 실패하면 한 번 올린다
}
_REPO: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")

# 서버가 처리하는 이벤트 (app/api/webhook.py의 parse_event). tests/setup이 둘이 같은지 본다
HOOK_EVENTS: Final = (
    "pull_request", "issue_comment", "pull_request_review", "pull_request_review_comment",
)  # fmt: skip
EVENT_LABELS: Final[Mapping[str, str]] = {
    "pull_request": "Pull requests",
    "issue_comment": "Issue comments",
    "pull_request_review": "Pull request reviews",
    "pull_request_review_comment": "Pull request review comments",
}
WEBHOOK_PATH: Final = "/webhook/github"

TOKEN_PERMISSIONS: Final = """토큰에 필요한 권한 (처리할 repo마다):
  fine-grained 토큰의 Repository permissions
    - Contents: Read and write   (clone·fetch, 수정안 브랜치 push와 지우기)
    - Pull requests: Read and write   (PR 조회, 수정안 PR 열기·닫기, 리뷰·줄 코멘트, 스레드 해결)
    - Issues: Read and write   (PR의 일반 코멘트 읽기·달기·고치기, PR이 닫는 이슈 읽기)
    - Metadata: Read-only   (자동으로 붙는다)
    - Webhooks: Read and write   (configure·add·rm이 웹훅을 등록·지울 때만)
  classic 토큰의 scope
    - repo   (공개 repo만 다루면 public_repo)
    - admin:repo_hook   (웹훅을 등록·지울 때만)"""


class ConfigEditError(ValueError):
    """config.yaml의 repos를 줄 단위로 고칠 수 없다. 손으로 고쳐야 한다."""


# 브라우저나 git에서 복사한 GitHub 주소: https://github.com/a/b, github.com/a/b.git,
# git@github.com:a/b.git
_GITHUB_URL: Final = re.compile(
    r"(?:(?:https?://)?(?:www\.)?github\.com/|git@github\.com:)([^/\s]+)/([^/\s?#]+)(?:[/?#].*)?",
    re.IGNORECASE,
)


def repo_name(text: str) -> str:
    """사용자가 적은 repo를 `계정/저장소이름`으로. GitHub 주소를 붙여 넣은 것도 받는다(끝의 `.git`과
    뒤의 경로는 뗀다). 주소가 아니면 앞뒤 공백만 뗀 그대로다(꼴은 `repo_problem`이 본다)."""
    text = text.strip()
    if found := _GITHUB_URL.fullmatch(text):
        return f"{found.group(1)}/{found.group(2).removesuffix('.git')}"
    return text


def repo_problem(text: str) -> str | None:
    """repo가 `계정/저장소이름` 꼴도 GitHub 주소도 아니면 그 안내."""
    name = repo_name(text)
    if _REPO.fullmatch(name) and name.split("/")[1] not in (".", ".."):
        return None
    return (
        f"repo는 `계정/저장소이름` 꼴이나 GitHub 주소로 적습니다 (예: {REPO_EXAMPLE}). "
        f"받은 값: {text!r}"
    )


def spelled_as(name: str, known: Sequence[str]) -> str | None:
    """known 중 name과 대소문자만 다른(또는 같은) 것. GitHub의 계정과 저장소 이름은 대소문자를
    가리지 않는다."""
    wanted = name.casefold()
    return next((k for k in known if k.casefold() == wanted), None)


# --- config.yaml ---


def _load(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        raise ConfigEditError(f"config.yaml을 읽지 못함: {e}") from e
    data = {} if data is None else data
    if not isinstance(data, dict):
        raise ConfigEditError("config.yaml의 맨 위가 키와 값의 묶음이 아님")
    repos = data.get("repos")
    if repos is not None and not (
        isinstance(repos, list) and all(isinstance(r, str) for r in repos)
    ):
        raise ConfigEditError("config.yaml의 repos가 글자의 목록이 아님")
    return data


def config_data(text: str) -> dict[str, Any]:
    """config.yaml의 글을 읽은 값. 읽지 못하거나 repos가 글자의 목록이 아니면 ConfigEditError."""
    return _load(text)


def config_repos(text: str) -> list[str]:
    return list(_load(text).get("repos") or [])


_REPOS_KEY: Final = re.compile(r"^repos\s*:")
_FLOW: Final = re.compile(r"^(repos\s*:\s*)\[(.*)\](\s*(?:#.*)?)$")
_BLOCK_HEAD: Final = re.compile(r"^repos\s*:\s*(#.*)?$")
_ITEM: Final = re.compile(r"^(\s*)-\s+(.*?)\s*(#.*)?$")


def _flow_items(inner: str) -> list[str]:
    return [item.strip() for item in inner.split(",") if item.strip()]


def _value(scalar: str) -> object:
    try:
        return yaml.safe_load(scalar)
    except yaml.YAMLError:
        return None


def _block_items(lines: list[str], head: int) -> list[int]:
    """`repos:` 아래 목록 항목의 줄 번호들. 빈 줄과 주석은 건너뛰고, 들여쓰지 않은 다른 키에서
    멈춘다."""
    found = []
    for i in range(head + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _ITEM.match(line.rstrip("\n")):
            found.append(i)
            continue
        if line[0] in " \t":  # 항목이 여러 줄이면 아래 확인에서 걸러진다
            continue
        break
    return found


def _check(before: dict[str, Any], text: str, repos: list[str]) -> str:
    after = _load(text)
    if after.get("repos") != repos or {k: v for k, v in after.items() if k != "repos"} != {
        k: v for k, v in before.items() if k != "repos"
    }:
        raise ConfigEditError("config.yaml의 repos를 고치지 못함. 손으로 고친다")
    return text


def add_repos(text: str, repos: Sequence[str]) -> tuple[str, list[str]]:
    """repos 목록에 없는 것만 끝에 더한다. (새 글, 더한 것). 더할 것이 없으면 글을 그대로
    돌려준다."""
    before = _load(text)
    current = list(before.get("repos") or [])
    new = []
    for r in dict.fromkeys(repos):  # 대소문자만 다른 것은 이미 있는 것이다
        if spelled_as(r, [*current, *new]) is None:
            new.append(r)
    if not new:
        return text, []
    lines = text.splitlines(keepends=True)
    head = next((i for i, line in enumerate(lines) if _REPOS_KEY.match(line)), None)
    if head is None:
        body = text if not text or text.endswith("\n") else text + "\n"
        out = body + "repos:\n" + "".join(f"  - {r}\n" for r in new)
        return _check(before, out, current + new), new
    line = lines[head].rstrip("\n")
    if flow := _FLOW.match(line):
        items = _flow_items(flow.group(2)) + new
        lines[head] = f"{flow.group(1)}[{', '.join(items)}]{flow.group(3)}\n"
    elif _BLOCK_HEAD.match(line):
        items = _block_items(lines, head)
        last = items[-1] if items else head
        indent = _ITEM.match(lines[last].rstrip("\n")).group(1) if items else "  "  # type: ignore[union-attr]
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines[last + 1 : last + 1] = [f"{indent}- {r}\n" for r in new]
    else:
        raise ConfigEditError(f"config.yaml의 repos 줄을 고칠 수 없음: {line!r}. 손으로 고친다")
    return _check(before, "".join(lines), current + new), new


def remove_repos(text: str, repos: Sequence[str]) -> tuple[str, list[str]]:
    """repos 목록에서 뺀다. (새 글, 뺀 것). 마지막 것을 빼면 `repos: []`로 둔다(빈 목록도 설정으로
    읽힌다. `repos:`만 남으면 null이라 서버가 시작하지 못한다)."""
    before = _load(text)
    current = list(before.get("repos") or [])
    # 목록에 적힌 철자로 찾는다(대소문자만 다르게 적어도 뺀다)
    gone = list(dict.fromkeys(s for r in repos if (s := spelled_as(r, current)) is not None))
    if not gone:
        return text, []
    left = [r for r in current if r not in gone]
    lines = text.splitlines(keepends=True)
    head = next((i for i, line in enumerate(lines) if _REPOS_KEY.match(line)), None)
    if head is None:
        raise ConfigEditError("config.yaml에서 repos 줄을 찾지 못함")
    line = lines[head].rstrip("\n")
    if flow := _FLOW.match(line):
        items = [item for item in _flow_items(flow.group(2)) if _value(item) not in gone]
        lines[head] = f"{flow.group(1)}[{', '.join(items)}]{flow.group(3)}\n"
    elif head_match := _BLOCK_HEAD.match(line):
        drop = [
            i for i in _block_items(lines, head)
            if _value(_ITEM.match(lines[i].rstrip("\n")).group(2)) in gone  # type: ignore[union-attr]
        ]  # fmt: skip
        for i in reversed(drop):
            del lines[i]
        if not left:
            comment = head_match.group(1)
            lines[head] = "repos: []" + (f"  {comment}" if comment else "") + "\n"
    else:
        raise ConfigEditError(f"config.yaml의 repos 줄을 고칠 수 없음: {line!r}. 손으로 고친다")
    return _check(before, "".join(lines), left), gone


# --- config.yaml의 묶음 아래 한 키 (model.default, rag.allowed) ---
#
# `repos`와 같이 글을 줄 단위로 고친다(YAML로 읽어 다시 쓰면 주석과 순서가 사라진다). 중첩된 키라
# 다루는 꼴을 좁힌다: 맨 위의 `<묶음>:` 줄이 블록의 머리(`<묶음>:` 뒤에 값이 없음)일 때 그 바로 아래
# 들여쓴 키 중 그 키 한 줄만 바꾸고, 넣고, 지운다. `model: {calls: ...}` 같은 한 줄 꼴은 고치지 않고
# 손으로 고치라고 한다(configure가 쓰지 않는 꼴이다). 고친 뒤 YAML로 읽어 그 키 밖이 그대로인지
# 확인한다.

RAG_ALLOWED_KEY: Final = "rag.allowed"  # 이 컴퓨터에서 검색을 쓸지 (`pr-proof rag on|off`)


def model_name_problem(name: str) -> str | None:
    """claude --model에 넘길 이름의 꼴이 아니면 그 안내. 그 이름의 모델이 실제로 있는지는 모델을
    불러야 알 수 있어 보지 않는다."""
    if re.fullmatch(MODEL_NAME_PATTERN, name):
        return None
    return (
        f"모델 이름의 꼴이 아닙니다: {name!r}. claude --model이 받는 이름을 적습니다 "
        f"(예: {MODEL_EXAMPLES})"
    )


def _section(data: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigEditError(f"config.yaml의 {name}이 키와 값의 묶음이 아님")
    return value


def config_default_model(text: str) -> str | None:
    """config.yaml의 `model.default`. 없으면 None. 글자가 아니면 ConfigEditError."""
    value = _section(_load(text), "model").get("default")
    if value is not None and not isinstance(value, str):
        raise ConfigEditError(f"config.yaml의 model.default가 글자가 아님: {value!r}")
    return value


def config_rag_allowed(text: str) -> bool | None:
    """config.yaml의 `rag.allowed`. 없으면 None(묻기 전의 설치). true·false가 아니면
    ConfigEditError."""
    value = _section(_load(text), "rag").get("allowed")
    if value is not None and not isinstance(value, bool):
        raise ConfigEditError(f"config.yaml의 {RAG_ALLOWED_KEY}가 true나 false가 아님: {value!r}")
    return value


def _children(lines: list[str], head: int) -> list[tuple[int, str]]:
    """`<묶음>:` 아래 들여쓴 줄의 (줄 번호, 들여쓰기). 빈 줄과 주석은 건너뛰고, 들여쓰지 않은 줄에서
    멈춘다."""
    found = []
    for i in range(head + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] not in " \t":
            break
        found.append((i, line[: len(line) - len(line.lstrip())]))
    return found


def _yaml_scalar(name: str) -> str:
    """YAML이 그대로 글자로 읽는 꼴. `123`, `true`처럼 다른 값으로 읽히면 따옴표로 감싼다(이름의
    꼴에 따옴표와 역슬래시가 없어 그대로 감쌀 수 있다)."""
    return name if yaml.safe_load(name) == name else f'"{name}"'


def _rendered(value: str | bool | None) -> str:
    assert value is not None  # 지울 때는 값을 쓰지 않는다
    return ("true" if value else "false") if isinstance(value, bool) else _yaml_scalar(value)


def _without(
    data: Mapping[str, Any], section: str, key: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    rest = {k: v for k, v in data.items() if k != section}
    inner = {k: v for k, v in _section(data, section).items() if k != key}
    return rest, inner


def _same(a: object, b: object) -> bool:
    return a == b and type(a) is type(b)  # True와 1을 같다고 보지 않는다


def _set_child(text: str, section: str, key: str, value: str | bool | None) -> str:
    """`<section>.<key>`를 value로 적는다. None이면 지운다. 바뀔 것이 없으면 글을 그대로 돌려준다.

    지워서 `<section>:` 아래가 비면 그 줄도 지운다(`model:`만 남으면 null이라 서버가 시작하지
    못한다). 줄 단위로 고칠 수 없는 꼴이면 ConfigEditError.
    """
    before = _load(text)
    if _same(_section(before, section).get(key), value):
        return text
    lines = text.splitlines(keepends=True)
    head_key = re.compile(rf"^{section}\s*:")
    head = next((i for i, line in enumerate(lines) if head_key.match(line)), None)
    if head is None:
        body = text if not text or text.endswith("\n") else text + "\n"
        out = body + f"{section}:\n  {key}: {_rendered(value)}\n"  # None이면 위에서 끝났다
        return _check_child(before, out, section, key, value)
    head_line = lines[head].rstrip("\n")
    if not (head_match := re.match(rf"^{section}\s*:\s*(#.*)?$", head_line)):
        raise ConfigEditError(
            f"config.yaml의 {section} 줄을 고칠 수 없음: {head_line!r}. 손으로 고친다 "
            f"({section} 아래를 한 줄에 적은 꼴은 고치지 않는다)"
        )
    key_line = re.compile(rf"^(\s+){key}\s*:\s*(.*?)\s*(#.*)?$")
    children = _children(lines, head)
    indent = children[0][1] if children else "  "
    direct = [i for i, own in children if own == indent]
    found = next((i for i in direct if key_line.match(lines[i].rstrip("\n"))), None)
    if value is None:
        assert found is not None  # 값이 있었으므로 그 줄이 있다. 없으면 아래 확인이 막는다
        del lines[found]
        if len(direct) == 1:
            comment = head_match.group(1)
            lines[head] = f"{comment}\n" if comment else ""
    elif found is not None:
        line_match = key_line.match(lines[found].rstrip("\n"))
        assert line_match is not None
        comment = line_match.group(3)
        lines[found] = (
            f"{line_match.group(1)}{key}: {_rendered(value)}"
            + (f"  {comment}" if comment else "")
            + "\n"
        )
    else:
        if not lines[head].endswith("\n"):
            lines[head] += "\n"
        lines.insert(head + 1, f"{indent}{key}: {_rendered(value)}\n")
    return _check_child(before, "".join(lines), section, key, value)


def _check_child(
    before: dict[str, Any], text: str, section: str, key: str, value: str | bool | None
) -> str:
    after = _load(text)
    if _without(after, section, key) != _without(before, section, key) or not _same(
        _section(after, section).get(key), value
    ):
        raise ConfigEditError(f"config.yaml의 {section}.{key}를 고치지 못함. 손으로 고친다")
    return text


def set_default_model(text: str, name: str | None) -> str:
    """`model.default`를 name으로 적는다. None이면 지운다. 다른 설정과 주석은 둔다."""
    if name is not None and (problem := model_name_problem(name)):
        raise ConfigEditError(problem)
    return _set_child(text, "model", "default", name)


def set_rag_allowed(text: str, value: bool) -> str:
    """`rag.allowed`를 적는다. 다른 설정(`rag.embed_model` 등)과 주석은 둔다."""
    return _set_child(text, "rag", "allowed", value)


def default_models_label() -> str:
    """파이프라인 쪽 값이 없을 때의 단계별 기본값을 짧게. 모든 단계가 같으면 "모든 단계 sonnet",
    다르면 "판단 sonnet, 수정 haiku→sonnet" 꼴이다."""
    models = ModelSettings()
    stages = (models.rules, models.checker, models.decide, models.judge, models.light, models.heavy)
    if len(set(stages)) == 1:
        return f"모든 단계 {models.judge}"
    return f"판단 {models.judge}, 수정 {models.light}→{models.heavy}"


def model_view(default: str | None, prefix: str) -> str:
    """`model`(보기)의 글: 파이프라인 쪽 값과 단계별로 실제 쓰일 모델. prefix는 안내에 쓸 명령
    앞부분(`files.command_prefix`)이다."""
    if default is None:
        head = f"파이프라인 쪽 모델: 정하지 않음 (단계별 기본값: {default_models_label()})"
    else:
        head = f"파이프라인 쪽 모델: {default} (config.yaml의 model.default, 모든 단계)"
    models = with_default_model(RepoConfig(), default).model
    width = max(_cells(label) for label in STAGE_LABELS.values()) + 2
    lines = [head, "", "단계별로 쓰일 모델:"]
    for stage in STAGE_MODELS:
        value = getattr(models, stage)
        if stage == "heavy" and value == models.light:
            value += " (수정 작성과 같은 모델이라 올리지 않는다)"
        lines.append(f"  {_pad(STAGE_LABELS[stage], width)}{value}")
    lines += [
        "",
        "저장소의 .pipeline.yaml에 단계별로 적은 모델(model.rules 등)은 이 값보다 앞섭니다.",
        f"바꾸기: {prefix} model <이름>   기본값으로: {prefix} model {DEFAULT_MODEL_WORD}",
    ]
    return "\n".join(lines)


# --- 과거 수정 기록 검색 (rag) ---

RAG_EXTRAS: Final = "pr-proof[rag]"
# chromadb, onnxruntime 등. 운영체제와 판마다 달라 어림만 적는다
RAG_PACKAGES_SIZE: Final = "패키지 수백 MB"


def model_megabytes(spec: EmbedModel) -> int:
    """받을 임베딩 모델 파일(토크나이저와 ONNX)의 크기, MB."""
    return round((spec.tokenizer.size + spec.onnx.size) / 1_000_000)


def rag_size_text(spec: EmbedModel) -> str:
    model = f"임베딩 모델(약 {model_megabytes(spec)}MB)"
    return f"선택 의존성 {RAG_EXTRAS}({RAG_PACKAGES_SIZE})와 {model}"


def rag_label(allowed: bool | None) -> str:
    if allowed is None:
        return "정하지 않음"
    return "켬" if allowed else "끔"


def rag_view(
    allowed: bool | None, extras: bool, model: bool, spec: EmbedModel, model_dir: str, prefix: str
) -> str:
    """`rag`(보기)의 글: 고른 값, 선택 의존성이 깔렸는지, 임베딩 모델을 받았는지. prefix는 안내에 쓸
    명령 앞부분이다."""
    lines = [
        f"과거 수정 기록 검색 (이 컴퓨터, config.yaml의 {RAG_ALLOWED_KEY}): {rag_label(allowed)}",
        f"선택 의존성 {RAG_EXTRAS}: {'깔림' if extras else '없음'}",
        f"임베딩 모델 {spec.name}: "
        + (f"받음 ({model_dir})" if model else f"받지 않음 (약 {model_megabytes(spec)}MB)"),
        "",
    ]
    if allowed is False:
        lines.append("이 컴퓨터에서는 어느 repo도 검색하지 않습니다.")
    else:
        lines.append("repo마다 .pipeline.yaml의 rag.enabled(기본 auto)가 검색할지 정합니다.")
        if not extras:
            lines.append("선택 의존성이 없어 검색해야 할 때도 검색 없이 검사합니다.")
    lines.append(f"켜기: {prefix} rag on   끄기: {prefix} rag off")
    return "\n".join(lines)


# --- 웹훅 ---


@dataclass(frozen=True)
class Hook:
    id: int
    url: str
    events: tuple[str, ...]
    active: bool
    content_type: str


@dataclass(frozen=True)
class HookPlan:
    """create: 새로 만든다. update: 같은 주소의 웹훅을 맞춘다. conflict: 같은 주소는 없고 pr-proof로
    보이는 다른 주소의 웹훅이 있다. 만들면 같은 소식이 두 번 오므로 만들지 않는다."""

    action: Literal["create", "update", "conflict"]
    hook_id: int | None
    changes: tuple[str, ...]
    others: tuple[str, ...] = ()


def _looks_like_pr_proof(url: str) -> bool:
    return url.startswith("https://smee.io/") or url.rstrip("/").endswith(WEBHOOK_PATH)


def plan_hook(hooks: Sequence[Hook], url: str) -> HookPlan:
    same = [h for h in hooks if h.url == url]
    if not same:
        others = tuple(h.url for h in hooks if _looks_like_pr_proof(h.url))
        if others:
            return HookPlan("conflict", None, (), others)
        return HookPlan("create", None, ("새 웹훅을 만든다",))
    hook = same[0]
    changes = []
    if set(hook.events) != set(HOOK_EVENTS):
        missing = [EVENT_LABELS[e] for e in HOOK_EVENTS if e not in hook.events]
        changes.append(
            "이벤트를 넷으로 맞춘다" + (f" (빠짐: {', '.join(missing)})" if missing else "")
        )
    if not hook.active:
        changes.append("꺼져 있던 웹훅을 켠다")
    if hook.content_type != "json":
        changes.append("Content type을 application/json으로")
    # GitHub은 저장한 비밀값을 돌려주지 않아 같은지 알 수 없다. 늘 설정 폴더의 값으로 맞춘다
    changes.append("비밀값을 설정 폴더의 webhook_secret으로 맞춘다")
    return HookPlan("update", hook.id, tuple(changes))


def hook_payload(url: str, secret: str, *, create: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "active": True,
        "events": list(HOOK_EVENTS),
        "config": {"url": url, "content_type": "json", "secret": secret, "insecure_ssl": "0"},
    }
    return {"name": "web", **payload} if create else payload


def hooks_at(hooks: Sequence[Hook], url: str) -> list[Hook]:
    """그 주소의 웹훅만 (rm이 지우는 것). 다른 주소의 웹훅은 건드리지 않는다."""
    return [h for h in hooks if h.url == url]


def hook_status(hooks: Sequence[Hook], url: str | None) -> str:
    if url is None:
        return "웹훅 받는 주소가 저장되어 있지 않음"
    same = hooks_at(hooks, url)
    if not same:
        return "웹훅 없음"
    hook = same[0]
    if not hook.active:
        return "웹훅 있음, 꺼져 있음"
    missing = [EVENT_LABELS[e] for e in HOOK_EVENTS if e not in hook.events]
    if missing:
        return f"웹훅 있음, 이벤트 빠짐: {', '.join(missing)}"
    return "웹훅 있음, 이벤트 맞음"


# `list --check`가 다른 서버를 찾으려고 보는 열린 PR 수 (최근에 고친 것부터)
OTHER_SERVER_PRS: Final = 10


@dataclass(frozen=True)
class SeenMarker:
    """열린 PR의 결과 코멘트 하나의 첫 줄(표시)과 그 코멘트를 마지막으로 고친 때(GitHub의 값)."""

    pr_number: int
    marker: str
    updated_at: str


def other_servers_text(seen: Sequence[SeenMarker], my_id: str | None) -> str | None:
    """결과 코멘트에서 찾은 다른 서버를 한 줄로. 없으면 None. my_id는 이 서버의 id(아직 없으면
    None: 이 서버가 단 표시가 없다)이고, 서버 정보가 없는 옛 표시는 세지 않는다.

    서버마다 마지막으로 처리한 코멘트의 판과 때를 보인다.
    """
    latest: dict[str, SeenMarker] = {}
    versions: dict[str, str] = {}
    for item in seen:
        owner = read_owner(item.marker)
        if owner is None or owner.server_id == my_id:
            continue
        known = latest.get(owner.server_id)
        if known is None or item.updated_at > known.updated_at:
            latest[owner.server_id] = item
            versions[owner.server_id] = owner.version or "알 수 없음"
    if not latest:
        return None
    parts = [
        f"서버 {sid} (판 {versions[sid]}, "
        f"마지막 처리 {_when(item.updated_at)} PR #{item.pr_number})"
        for sid, item in sorted(latest.items(), key=lambda kv: kv[1].updated_at, reverse=True)
    ]
    return "다른 pr-proof 서버가 처리하고 있음: " + ", ".join(parts)


def _when(iso: str) -> str:
    """GitHub의 시각(`2026-11-24T10:05:00Z`)을 `2026-11-24 10:05 UTC`로."""
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?Z", iso)
    return f"{m.group(1)} {m.group(2)} UTC" if m else iso


def manual_hook_text(repo: str, url: str, secret_path: str) -> str:
    events = ", ".join(EVENT_LABELS[e] for e in HOOK_EVENTS)
    return "\n".join([
        f"{repo}의 GitHub 설정 → Webhooks → Add webhook에서 손으로 등록하세요:",
        f"  Payload URL: {url}",
        "  Content type: application/json",
        f"  Secret: {secret_path} 파일의 값",
        f"  이벤트(Let me select individual events): {events}",
    ])  # fmt: skip


# --- 안내 글 ---


def platform_key(platform: str) -> str:
    return platform if platform in ("darwin", "linux") else "other"


def missing_programs_text(
    missing: Sequence[str], hints: Mapping[str, Mapping[str, str]], platform: str
) -> str:
    """빠진 프로그램을 한 번에 모두, 운영체제에 맞는 설치 방법과 함께 (bin/pr-proof.js와 같은
    표)."""
    key = platform_key(platform)
    lines = ["필요한 프로그램이 없습니다. 설치한 뒤 다시 실행하세요.", ""]
    lines += [f"- {hints[name]['label']}: {hints[name][key]}" for name in missing]
    return "\n".join(lines)


def _cells(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글 같은 넓은 글자는 두 칸이다."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _cells(text))


def check_table(rows: Sequence[tuple[str, bool, str]]) -> str:
    """끝 확인 표. (이름, 통과, 고치는 방법)."""
    width = max(_cells(name) for name, _, _ in [("확인", True, ""), *rows]) + 2
    lines = [f"{_pad('확인', width)}결과"]
    for name, ok, hint in rows:
        lines.append(_pad(name, width) + ("통과" if ok else f"실패 → {hint}"))
    return "\n".join(lines)
