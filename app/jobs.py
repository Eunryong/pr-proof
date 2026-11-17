"""api·sync와 worker 사이의 작업 계약. 프로세스 사이에는 이 모델만 넘긴다."""

from typing import Literal

from pydantic import BaseModel, model_validator


class PRJob(BaseModel):
    repo: str  # "owner/name"
    pr_number: int
    kind: Literal["update", "command", "close"]
    head_sha: str | None = None  # issue_comment 웹훅에는 없다. 없으면 워커가 조회한다
    base_sha: str | None = None
    command: str | None = None  # "/recheck", "/mutate", "/generate-tests", "/fix lint" ...
    comment_id: int | None = None  # 명령의 코멘트 ID
    source: Literal["webhook", "sync"]
    delivery_id: str | None = None  # 웹훅으로 온 작업에만 있다

    @model_validator(mode="after")
    def _command_fields(self) -> "PRJob":
        if self.kind == "command" and (self.command is None or self.comment_id is None):
            raise ValueError("command 작업에는 command와 comment_id가 필요하다")
        if self.kind != "command" and (self.command is not None or self.comment_id is not None):
            raise ValueError("command와 comment_id는 command 작업에만 쓴다")
        return self

    @property
    def dedup_key(self) -> str:
        """대기·실행 중인 같은 작업을 다시 등록하지 않기 위한 키."""
        if self.kind == "command":
            return f"{self.repo}#{self.pr_number}#command#{self.comment_id}"
        return f"{self.repo}#{self.pr_number}#{self.head_sha}#{self.kind}"
