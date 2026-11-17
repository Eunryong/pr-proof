"""`command` 작업: PR 코멘트 명령 처리 (docs/DESIGN.md F11).

- `/recheck`: PR의 지금 head로 다시 검사한다
- `/fix lint`: 승인을 기다리며 멈춰 있는 수정 단계를 재개한다 (다른 스킬은 3·4단계)
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


def fix_unsupported_message(word: str, skill: str) -> str:
    target = f"{word} {skill}".strip()
    return (
        f"> {target}\n\n`{target}`는 아직 지원하지 않습니다. 지금은 `/fix lint`만 쓸 수 있습니다."
    )


def no_pending_fix_message(head_sha: str) -> str:
    return (
        f"> /fix lint\n\n이 커밋(`{head_sha[:7]}`)에는 승인을 기다리는 lint 수정이 없습니다. "
        "자동으로 고칠 수 있는 위반이 없거나, 테스트가 통과하지 않았거나, "
        "이미 처리했을 수 있습니다."
    )


def process_command(job: PRJob, deps: Deps) -> str:
    assert job.command is not None and job.comment_id is not None
    if deps.store.command_handled(job.comment_id):
        return "duplicate"
    parsed = parse_command(job.command)
    if parsed.name == "unknown":
        return "unknown_command"
    _, final = retry.attempt()
    try:
        refs = deps.github.pr_refs(job.repo, job.pr_number)
        if not refs.open:
            result = "closed"
        elif parsed.name == "recheck":
            result = f"recheck:{update.process_update(_target(job, refs), deps)}"
        elif parsed.name == "fix":
            result = _fix(job, deps, parsed.word, parsed.arg, _target(job, refs))
        else:
            result = _extension(job, deps, parsed.word, parsed.arg, refs.base_sha)
    except Exception:  # 환경 오류든 버그든 마지막 시도면 키를 지운다. 재시도는 RQ가 한다
        if final:
            deps.store.release(job)
        raise
    deps.store.record_command(job, result)
    return result


def _target(job: PRJob, refs) -> PRJob:
    """명령이 가리키는 PR의 지금 커밋에 대한 update 작업."""
    return PRJob(
        repo=job.repo,
        pr_number=job.pr_number,
        kind="update",
        head_sha=refs.head_sha,
        base_sha=refs.base_sha,
        source=job.source,
    )


def _fix(job: PRJob, deps: Deps, word: str, skill: str, target: PRJob) -> str:
    if skill != "lint":
        deps.github.reply(job.repo, job.pr_number, fix_unsupported_message(word, skill))
        return "fix_unsupported"
    if not update.has_pending_fix(target, deps):
        deps.github.reply(job.repo, job.pr_number, no_pending_fix_message(target.head_sha))
        return "no_pending_fix"
    approval = {"skill": skill, "comment_id": job.comment_id}
    return update.resume_fix(target, deps, approval)


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
