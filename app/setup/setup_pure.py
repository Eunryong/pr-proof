"""configure·add·rm·list의 판정과 글 (외부 호출 없음).

- repo 이름 확인
- config.yaml 고치기: `repos`에 더하고 빼되 다른 설정과 주석을 지킨다(글을 줄 단위로 고치고, 고친 뒤
  YAML로 읽어 바뀐 것이 `repos`뿐인지 확인한다)
- 웹훅: 만들지 고칠지, 이벤트, 손으로 등록할 값
- 빠진 프로그램 안내, 끝 확인 표, 토큰 권한 안내
"""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

import yaml

REPO_EXAMPLE: Final = "octocat/hello-world"
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
    - Issues: Read and write   (PR의 일반 코멘트 읽기·달기·고치기)
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
