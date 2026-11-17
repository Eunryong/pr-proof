"""`command` 작업: PR 코멘트 명령 처리 (docs/DESIGN.md F11).

- `/recheck`: PR의 지금 head로 다시 검사한다
- `/fix <skill>`: 멈춰 있는 수정 단계를 재개한다 (체크포인트와 함께 다음에 붙인다)
- 확장 명령(`/mutate`, `/generate-tests`): base 브랜치 설정에서 꺼져 있으면 꺼져 있다고 답한다
- 모르는 명령은 답하지 않는다 (`/cc` 같은 다른 관례일 수 있다)

처리한 코멘트 ID는 runs와 따로 저장해 같은 코멘트에 두 번 답하지 않는다.
"""

import logging

from app.models.command import parse_command
from app.models.job import PRJob
from app.models.repo_config import parse_repo_config
from app.worker import repo, retry, update
from app.worker.base_config import PIPELINE_YAML
from app.worker.deps import Deps

log = logging.getLogger(__name__)


def extension_off_message(word: str, name: str) -> str:
    return f"> {word}\n\n`{word}`는 이 리포에서 꺼져 있습니다 (`extensions.{name}.enabled`)."


def extension_unsupported_message(word: str) -> str:
    return f"> {word}\n\n`{word}`는 아직 이 파이프라인에서 지원하지 않습니다."


def process_command(job: PRJob, deps: Deps) -> str:
    assert job.command is not None and job.comment_id is not None
    if deps.store.command_handled(job.comment_id):
        return "duplicate"
    parsed = parse_command(job.command)
    if parsed.name == "unknown":
        return "unknown_command"
    if parsed.name == "fix":
        return "fix_not_ready"  # interrupt·재개와 함께 붙인다. 기록하지 않아 그때 다시 받을 수 있다

    _, final = retry.attempt()
    try:
        refs = deps.github.pr_refs(job.repo, job.pr_number)
        if not refs.open:
            result = "closed"
        elif parsed.name == "recheck":
            target = PRJob(
                repo=job.repo,
                pr_number=job.pr_number,
                kind="update",
                head_sha=refs.head_sha,
                base_sha=refs.base_sha,
                source=job.source,
            )
            result = f"recheck:{update.process_update(target, deps)}"
        else:
            result = _extension(job, deps, parsed.word, parsed.arg, refs.base_sha)
    except retry.EnvironmentFailure:
        if final:
            deps.store.release(job)
        raise
    deps.store.record_command(job, result)
    return result


def _extension(job: PRJob, deps: Deps, word: str, name: str, base_sha: str) -> str:
    files = repo.read_base_files_from_cache(
        deps.settings.data_dir, job.repo, base_sha, (PIPELINE_YAML,)
    )
    config, _ = parse_repo_config(files.get(PIPELINE_YAML))  # 읽지 못하면 기본값(꺼짐)
    if config.extension_enabled(name):
        body, result = extension_unsupported_message(word), "extension_unsupported"
    else:
        body, result = extension_off_message(word, name), "extension_off"
    deps.github.reply(job.repo, job.pr_number, body)
    return result
