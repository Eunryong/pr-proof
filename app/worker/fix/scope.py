"""AI 수정의 허용 범위 검사 (docs/DESIGN.md F7 범위 검사 방법·무시된 파일).

git은 호스트에서 실행한다. 판정은 `app/worker/fix/scope_pure.py`가 한다.

순서: AI를 처음 실행하기 직전에 `snapshot_ignored`로 스냅샷을 남기고, AI 실행이 끝날 때마다
샌드박스에서 코드를 실행하기 전에 `check_scope`로 확인한다. 재시도 사이에 스냅샷을 다시 찍지 않는다
(이전 회차에 AI가 만든 무시된 파일이 '이미 있던 파일'이 되지 않게).
"""

from pathlib import Path

from app.worker.fix.scope_pure import (
    AI_POLICY,
    ScopePolicy,
    ScopeResult,
    judge_scope,
    parse_ignored,
    parse_porcelain,
)
from app.worker.git import repo


def snapshot_ignored(worktree: Path) -> frozenset[str]:
    """지금 있는 무시된 파일 (리포 기준 경로). 파이프라인 산출물은 뺀다."""
    return parse_ignored(repo.ignored_status(worktree))


def check_scope(
    worktree: Path,
    allowed: set[str],
    ignored_before: frozenset[str],
    *,
    policy: ScopePolicy = AI_POLICY,
) -> ScopeResult:
    """worktree의 모든 변경과 새로 생긴 무시된 파일이 허용 범위 안인지. git 실패는 GitError."""
    entries = parse_porcelain(repo.status(worktree))
    # 지금 심볼릭 링크인 바뀐 경로. 삭제된 경로는 없으므로 False다
    symlinks = frozenset(e.path for e in entries if (worktree / e.path).is_symlink())
    new_ignored = snapshot_ignored(worktree) - ignored_before
    return judge_scope(entries, allowed, policy, new_ignored=new_ignored, symlinks=symlinks)
