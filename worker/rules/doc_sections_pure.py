"""큰 규칙 문서의 관련 절만 읽기 (docs/DESIGN.md 6.3). 조각 나누기, 늘 읽는 절, 고르기, 발췌, 절
단위 캐시 키. 판단만 하고 임베딩이나 모델 호출은 하지 않는다 (worker/rules/rules.py).

규칙 문서 합계가 400KB를 넘고 검색을 켠 저장소만 이 방식을 쓴다. 문서를 제목 단위 조각(제목 줄과 그
아래 하위 절을 뺀 본문)으로 나눠 임베딩하고, 그 PR과 관련된 조각만 모델이 읽는다. 조각을 모은 발췌는
원래 문서의 줄을 그대로 쓰고 조각의 상위 제목 줄을 남겨, 원문 확인(`rule_reading.verify_rules`)과
규칙 id·출처가 문서 전체를 읽을 때와 같다.
"""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from worker.cache_key_pure import model_cache_key
from worker.rule_pure import Rule, RuleSet, in_scope
from worker.rule_reading_pure import (
    MAX_DOCS_BYTES,
    PROMPT_VERSION,
    READER_VERSION,
    SECTION_SEPARATOR,
    Document,
    headings,
)

# 큰 문서 방식에서 읽어 조각으로 나눌 문서 합계의 상한. 넘는 문서는 읽지 않는다
# (임베딩 시간과 메모리)
MAX_LARGE_DOCS_BYTES: Final = 4_000_000
# 제목에 이 말이 든 절은 검색과 관계없이 늘 읽는다 (6.3). 문서의 머리 부분도 늘 읽는다
ALWAYS_WORDS: Final = ("규칙", "컨벤션", "스타일", "contributing")
MAX_QUERY_CHARS: Final = 4000
MAX_EMBED_CHARS: Final = 4000


@dataclass(frozen=True)
class Chunk:
    """제목 하나의 조각: 제목 줄과 그 절의 본문(하위 절 제외). heading이 ()면 첫 제목 앞의
    머리다."""

    path: str
    blob_sha: str
    heading: tuple[str, ...]
    start: int  # 조각의 첫 줄 (0부터, 제목 줄)
    end: int  # 다음 제목 줄 (포함하지 않음)
    keep: tuple[int, ...]  # 발췌에 같이 남길 줄: 문서 이름 제목과 상위 절의 제목 줄
    text: str

    @property
    def id(self) -> str:
        """조각의 내용 해시. 문서의 다른 곳이 바뀌어도 이 조각이 그대로면 다시 임베딩하지 않는다."""
        raw = "\0".join((self.path, SECTION_SEPARATOR.join(self.heading), self.text))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    @property
    def label(self) -> str:
        return (
            f"{self.path} > {SECTION_SEPARATOR.join(self.heading)}"
            if self.heading
            else (f"{self.path} (머리)")
        )

    @property
    def size(self) -> int:
        return len(self.text.encode())

    @property
    def embed_text(self) -> str:
        return f"{self.label}\n{self.text}"[:MAX_EMBED_CHARS]


def chunks(document: Document) -> tuple[Chunk, ...]:
    """문서를 제목 단위 조각으로. 모든 줄이 정확히 한 조각에 든다."""
    lines = document.text.splitlines()
    top, found = headings(document.text)
    title = (top[0],) if top is not None else ()
    first = found[0][0] if found else len(lines)
    out = [
        Chunk(document.path, document.blob_sha, (), 0, first, (),
              "\n".join(lines[:first]))
    ]  # fmt: skip
    stack: list[tuple[int, int, str]] = []  # (단계, 줄, 제목)
    for n, (line_no, level, name) in enumerate(found):
        while stack and stack[-1][0] >= level:
            stack.pop()
        keep = title + tuple(s[1] for s in stack)
        stack.append((level, line_no, name))
        end = found[n + 1][0] if n + 1 < len(found) else len(lines)
        out.append(Chunk(document.path, document.blob_sha, tuple(s[2] for s in stack), line_no,
                         end, keep, "\n".join(lines[line_no:end])))  # fmt: skip
    return tuple(c for c in out if c.text.strip() or c.heading)


def always_read(chunk: Chunk) -> bool:
    """문서의 머리와, 제목 경로에 규칙·컨벤션·스타일·contributing이 든 절 (6.3)."""
    if not chunk.heading:
        return True
    title = " ".join(chunk.heading).casefold()
    return any(word in title for word in ALWAYS_WORDS)


def excerpt(document: Document, wanted: Iterable[Chunk]) -> Document:
    """고른 조각만 남긴 문서. 줄은 원래 순서이고, 조각의 상위 제목 줄과 문서 이름 제목을 같이 남겨
    절 경로가 문서 전체에서와 같다. 경로와 blob 해시는 원래 문서의 것이다."""
    lines = document.text.splitlines()
    kept: set[int] = set()
    for c in wanted:
        if c.path != document.path:
            continue
        kept |= set(range(c.start, c.end)) | set(c.keep)
    return Document(document.path, document.blob_sha, "\n".join(lines[i] for i in sorted(kept)))


def section_key(chunk: Chunk, model: str) -> str:
    """조각 하나의 규칙을 저장하는 키 (6.3 "절 단위"). 같은 조각은 다시 읽지 않는다."""
    parts = {"reader": READER_VERSION, "chunk": chunk.id, "path": chunk.path}
    return model_cache_key("rules-section", model, PROMPT_VERSION, parts)


def query_text(changed: Sequence[str], added: str) -> str:
    """조각을 찾는 질의 (6.3 "꺼내는 방법"): PR이 바꾼 파일의 경로와 폴더, 바뀐 hunk의 추가된 줄."""
    folders = sorted({p.rsplit("/", 1)[0] for p in changed if "/" in p})
    head = "바뀐 파일: " + ", ".join(changed) + "\n폴더: " + ", ".join(folders)
    return f"{head}\n바뀐 코드:\n{added}"[:MAX_QUERY_CHARS]


def scoped_to(chunk_rules: Mapping[str, RuleSet], changed: Sequence[str]) -> set[str]:
    """이미 읽어 둔 조각 중 그 규칙의 적용 범위(문서가 밝힌 것)가 바뀐 파일에 걸리는 것 (검색이
    아니라 저장한 규칙으로 고른다)."""
    out = set()
    for chunk_id, ruleset in chunk_rules.items():
        if any(r.scope and any(in_scope(r.scope, f) for f in changed) for r in ruleset.rules):
            out.add(chunk_id)
    return out


def pick(
    all_chunks: Sequence[Chunk],
    *,
    scoped: set[str],
    hits: Sequence[tuple[str, float]],
    max_bytes: int = MAX_DOCS_BYTES,
) -> tuple[Chunk, ...]:
    """읽을 조각: 늘 읽는 절, 적용 범위로 고른 절, 검색이 찾은 절(hits, `rag.top_k`개, 유사도 순)의
    차례로 합계가 max_bytes 안에 드는 것까지. 결과는 문서와 줄 순서다.

    조각에는 기록의 유사도 기준값을 쓰지 않는다. 질의(경로와 바뀐 줄)와 절의 글은 모양이 달라
    유사도가 기록끼리보다 낮다: multilingual-e5-small에서 맞는 절이 1위였지만 0.893으로 기본
    기준값 0.91에 못 미쳤다 (5단계, 11.1)."""
    by_id = {c.id: c for c in all_chunks}
    order = [c.id for c in all_chunks if always_read(c)]
    order += [c.id for c in all_chunks if c.id in scoped]
    order += [i for i, _ in sorted(hits, key=lambda h: -h[1]) if i in by_id]
    chosen: dict[str, Chunk] = {}
    total = 0
    for chunk_id in order:
        chunk = by_id[chunk_id]
        if chunk_id in chosen or total + chunk.size > max_bytes:
            continue
        chosen[chunk_id] = chunk
        total += chunk.size
    position = {c.id: n for n, c in enumerate(all_chunks)}
    return tuple(sorted(chosen.values(), key=lambda c: position[c.id]))


def chunk_of(rule: Rule, among: Sequence[Chunk]) -> Chunk | None:
    """규칙이 나온 조각: 같은 문서이고 원문 확인이 찾은 절이 그 조각의 제목 경로인 것."""
    path = tuple(p for p in rule.source.section.split(SECTION_SEPARATOR) if p)
    return next((c for c in among if c.path == rule.source.file and c.heading == path), None)
