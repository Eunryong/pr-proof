"""로컬 모델 구현이 쓰는 안전한 파일 도구 (docs/DESIGN.md 8.4).

`workdir.model_folder`가 worktree의 일반 파일만 복사한 폴더(`root`) 안에서만 동작한다.
- 폴더 밖의 절대 경로, `..` 탈출, 심볼릭 링크, `.git` 접근을 모두 거부하고 `denials`에 남긴다.
- 명령 실행(shell/subprocess)과 네트워크 도구가 없다.
- 읽기 도구가 본 경로를 `reads`에 폴더 기준 상대 경로(폴더 전체는 `"."`)로 남긴다.
"""

import fnmatch
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Final

from app.worker.model.local import openai_pure
from app.worker.model.model_backend import PermissionDenial

MAX_READ_BYTES: Final = 2 * 1024 * 1024
DEFAULT_READ_LIMIT: Final = 200
MAX_READ_LIMIT: Final = 1000
MAX_GREP_MATCHES: Final = 200
MAX_GLOB_RESULTS: Final = 200
_WILDCARDS: Final = "*?[{"


def _inside(root: Path, raw_path: str) -> Path | None:
    """raw_path의 실제 위치가 root 안이고 `.git`이 아니면 그 Path, 아니면 None."""
    if not raw_path or "\0" in raw_path:
        return None
    root_str = str(root)
    real = Path(os.path.realpath(os.path.join(root_str, raw_path)))
    real_str = str(real)
    if real_str != root_str and not real_str.startswith(root_str + os.sep):
        return None
    if real != root:
        rel_parts = real.relative_to(root).parts
        if rel_parts and rel_parts[0] == ".git":
            return None
    return real


def _pattern_escapes(root: Path, pattern: str) -> bool:
    """glob 패턴이 root 밖을 볼 수 있는 모양인지 (`path_hook.pattern_escapes`와 같은 규칙)."""
    if not pattern or "\0" in pattern:
        return True
    parts = pattern.replace("\\", "/").split("/")
    if ".." in parts:
        return True
    literal: list[str] = []
    for part in parts:
        if any(c in part for c in _WILDCARDS):
            break
        literal.append(part)
    prefix = "/".join(literal)
    return bool(prefix) and _inside(root, prefix) is None


def _rel_label(root: Path, target: Path) -> str:
    return "." if target == root else target.relative_to(root).as_posix()


def _has_symlink_component(root: Path, target: Path) -> bool:
    """root 아래에서 target까지의 경로에 심볼릭 링크가 있는지."""
    if target == root:
        return False
    cur = root
    for part in target.relative_to(root).parts:
        cur = cur / part
        try:
            mode = os.lstat(cur).st_mode
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if stat.S_ISLNK(mode):
            return True
    return False


def _read_regular_text(target: Path) -> str | None:
    """일반 파일만 O_NOFOLLOW로 열어 UTF-8 텍스트로 읽는다. 링크·특수 파일·너무 큰 파일은 None."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_READ_BYTES:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(MAX_READ_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > MAX_READ_BYTES:
        return None
    return data.decode("utf-8", errors="replace")


def _walk_regular_files(root: Path, base: Path) -> list[tuple[str, Path]]:
    """base 아래의 일반 파일들을 (root 기준 POSIX 상대 경로, Path) 목록으로 반환한다."""
    if base.is_file():
        if _has_symlink_component(root, base):
            return []
        return [(_rel_label(root, base), base)]
    found: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        cur_dir = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and not (cur_dir / d).is_symlink())
        for name in sorted(filenames):
            file_path = cur_dir / name
            if file_path.is_symlink():
                continue
            found.append((_rel_label(root, file_path), file_path))
    return found


def _match_glob(rel_posix: str, pattern: str) -> bool:
    norm = pattern.replace("\\", "/").lstrip("./")
    if not norm:
        return False
    if "/" not in norm:
        return fnmatch.fnmatch(PurePosixPath(rel_posix).name, norm)
    if PurePosixPath(rel_posix).match(norm):
        return True
    return fnmatch.fnmatch(rel_posix, norm)


class SandboxTools:
    """모델 전용 작업 폴더(`root`) 안에서만 동작하는 파일 도구 실행기."""

    def __init__(self, root: Path, *, allow_write: bool) -> None:
        self._root = root.resolve()
        self._allow_write = allow_write
        self.reads: set[str] = set()
        self.tool_uses: int = 0
        self.denials: list[PermissionDenial] = []

    def _deny(self, tool_name: str, args: Mapping[str, object], reason: str) -> str:
        self.denials.append(PermissionDenial(tool_name, dict(args)))
        return f"거부됨: {reason}"

    def execute(self, name: str, args: Mapping[str, object]) -> str:
        if name == openai_pure.READ_FILE:
            return self._read_file(args)
        if name == openai_pure.GREP:
            return self._grep(args)
        if name == openai_pure.GLOB:
            return self._glob(args)
        if name == openai_pure.WRITE_FILE:
            if not self._allow_write:
                return self._deny(name, args, "이 호출에서는 쓰기 도구를 쓸 수 없다")
            return self._write_file(args)
        if name == openai_pure.EDIT_FILE:
            if not self._allow_write:
                return self._deny(name, args, "이 호출에서는 쓰기 도구를 쓸 수 없다")
            return self._edit_file(args)
        return self._deny(name, args, f"허용 목록에 없는 도구: {name}")

    def _read_file(self, args: Mapping[str, object]) -> str:
        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return self._deny(openai_pure.READ_FILE, args, "path가 없거나 문자열이 아님")
        target = _inside(self._root, raw_path)
        if target is None or _has_symlink_component(self._root, target):
            return self._deny(openai_pure.READ_FILE, args, "작업 폴더 밖의 경로 또는 심볼릭 링크")
        self.tool_uses += 1
        text = _read_regular_text(target)
        if text is None:
            return f"파일을 읽을 수 없음: {raw_path}"
        self.reads.add(_rel_label(self._root, target))
        lines = text.splitlines()
        raw_offset = args.get("offset", 1)
        raw_limit = args.get("limit", DEFAULT_READ_LIMIT)
        offset = raw_offset if isinstance(raw_offset, int) and raw_offset >= 1 else 1
        limit = (
            min(raw_limit, MAX_READ_LIMIT)
            if isinstance(raw_limit, int) and raw_limit >= 1
            else DEFAULT_READ_LIMIT
        )
        sliced = lines[offset - 1 : offset - 1 + limit]
        numbered = [f"{offset + i}: {line}" for i, line in enumerate(sliced)]
        return "\n".join(numbered) if numbered else "(빈 파일 또는 범위 밖)"

    def _grep(self, args: Mapping[str, object]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return "오류: pattern이 비어 있음"
        raw_path = args.get("path")
        if raw_path is None or raw_path == "":
            base = self._root
        elif isinstance(raw_path, str):
            base = _inside(self._root, raw_path)
            if base is None or _has_symlink_component(self._root, base):
                return self._deny(openai_pure.GREP, args, "작업 폴더 밖의 경로 또는 심볼릭 링크")
        else:
            return self._deny(openai_pure.GREP, args, "path가 문자열이 아님")
        raw_glob = args.get("glob")
        if raw_glob is not None:
            if not isinstance(raw_glob, str) or _pattern_escapes(self._root, raw_glob):
                return self._deny(openai_pure.GREP, args, "작업 폴더 밖을 보는 glob 패턴")
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"오류: 잘못된 정규표현식 ({e})"
        self.tool_uses += 1
        if not base.exists():
            return "일치 없음"
        self.reads.add(_rel_label(self._root, base))
        hits: list[str] = []
        for rel, file_path in _walk_regular_files(self._root, base):
            if raw_glob is not None and not _match_glob(rel, raw_glob):
                continue
            text = _read_regular_text(file_path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if compiled.search(line):
                    hits.append(f"{rel}:{lineno}: {line}")
                    if len(hits) >= MAX_GREP_MATCHES:
                        hits.append("… (일치 상한에 닿음)")
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "일치 없음"

    def _glob(self, args: Mapping[str, object]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or _pattern_escapes(self._root, pattern):
            return self._deny(openai_pure.GLOB, args, "작업 폴더 밖을 보는 패턴")
        raw_path = args.get("path")
        if raw_path is None or raw_path == "":
            base = self._root
        elif isinstance(raw_path, str):
            base = _inside(self._root, raw_path)
            if base is None or _has_symlink_component(self._root, base):
                return self._deny(openai_pure.GLOB, args, "작업 폴더 밖의 경로 또는 심볼릭 링크")
        else:
            return self._deny(openai_pure.GLOB, args, "path가 문자열이 아님")
        self.tool_uses += 1
        if not base.exists():
            return "일치 없음"
        self.reads.add(_rel_label(self._root, base))
        matched: list[str] = []
        for rel, _ in _walk_regular_files(self._root, base):
            rel_from_base = (
                rel
                if base == self._root
                else Path(rel).relative_to(base.relative_to(self._root)).as_posix()
            )
            if _match_glob(rel_from_base, pattern):
                matched.append(rel)
                if len(matched) >= MAX_GLOB_RESULTS:
                    matched.append("… (결과 상한에 닿음)")
                    break
        return "\n".join(matched) if matched else "일치 없음"

    def _write_file(self, args: Mapping[str, object]) -> str:
        raw_path = args.get("path")
        content = args.get("content")
        if not isinstance(raw_path, str) or not raw_path:
            return self._deny(openai_pure.WRITE_FILE, args, "path가 없거나 문자열이 아님")
        if not isinstance(content, str):
            return "오류: content가 문자열이 아님"
        target = _inside(self._root, raw_path)
        if target is None or target == self._root or _has_symlink_component(self._root, target):
            return self._deny(openai_pure.WRITE_FILE, args, "작업 폴더 밖의 경로 또는 심볼릭 링크")
        self.tool_uses += 1
        target.parent.mkdir(parents=True, exist_ok=True)
        if _has_symlink_component(self._root, target):
            return self._deny(openai_pure.WRITE_FILE, args, "심볼릭 링크에는 쓸 수 없음")
        target.write_text(content, encoding="utf-8")
        return f"작성함: {_rel_label(self._root, target)} ({len(content)}자)"

    def _edit_file(self, args: Mapping[str, object]) -> str:
        raw_path = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        replace_all = args.get("replace_all", False)
        if not isinstance(raw_path, str) or not raw_path:
            return self._deny(openai_pure.EDIT_FILE, args, "path가 없거나 문자열이 아님")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return "오류: old_string과 new_string은 문자열이어야 함"
        if not old_string:
            return "오류: old_string이 비어 있음"
        target = _inside(self._root, raw_path)
        if target is None or target == self._root or _has_symlink_component(self._root, target):
            return self._deny(openai_pure.EDIT_FILE, args, "작업 폴더 밖의 경로 또는 심볼릭 링크")
        self.tool_uses += 1
        text = _read_regular_text(target)
        if text is None:
            return f"파일을 읽을 수 없음: {raw_path}"
        count = text.count(old_string)
        if count == 0:
            return f"오류: old_string을 찾지 못함 ({raw_path})"
        if count > 1 and not replace_all:
            return f"오류: old_string이 {count}곳에서 일치함 (더 긴 문맥을 주거나 replace_all=true)"
        updated = (
            text.replace(old_string, new_string)
            if replace_all
            else text.replace(old_string, new_string, 1)
        )
        target.write_text(updated, encoding="utf-8")
        return f"수정함: {_rel_label(self._root, target)}"
