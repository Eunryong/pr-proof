"""배포 직후의 시작 순서: 워커가 작업을 받기 전에 DB 스키마를 맞추고, 웹 쪽 동기화가 그 DB를 읽는다.

웹 쪽은 읽기만 하므로 배포가 더한 열은 워커가 만든다. 워커가 첫 작업에서야 만들면 그 사이 동기화가
주기마다 실패한다 (2026-11-20 배포, `no such column: run.author_association`).
"""

import asyncio
import logging
from pathlib import Path
from typing import Final

import fakeredis
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from app.api import main as api
from app.api import sync
from app.api.github import OpenPR
from app.api.history import History
from app.shared import config
from app.worker import main, preflight

HEAD: Final = "h" * 40
NEW_HEAD: Final = "n" * 40
# 6단계 전의 `run` 테이블. 작성자 열 네 개가 없다
OLD_RUN: Final = (
    "CREATE TABLE run (id INTEGER PRIMARY KEY, repo VARCHAR NOT NULL, "
    "pr_number INTEGER NOT NULL, head_sha VARCHAR NOT NULL, kind VARCHAR NOT NULL, "
    "source VARCHAR NOT NULL, state VARCHAR NOT NULL, test_status VARCHAR, "
    "comment_id INTEGER, started_at DATETIME NOT NULL, "
    "heartbeat_at DATETIME NOT NULL, finished_at DATETIME)"
)
OLD_ROW: Final = (
    "INSERT INTO run (repo, pr_number, head_sha, kind, source, state, started_at, heartbeat_at) "
    f"VALUES ('o/r', 1, '{HEAD}', 'update', 'webhook', 'done', "
    "'2026-11-19 00:00:00', '2026-11-19 00:00:00')"
)


@pytest.fixture
def deploy(tmp_path: Path, monkeypatch):
    """운영자의 설정 폴더 대신 임시 폴더. 워커는 preflight를 지나 큐를 기다리는 자리에서 멈춘다."""
    config_dir, data_dir = tmp_path / "config", tmp_path / "data"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(f"repos: [o/r]\ndata_dir: {data_dir}\n")
    monkeypatch.setenv(config.CONFIG_DIR_ENV, str(config_dir))
    monkeypatch.setattr(preflight, "run_checks", lambda redis: [])
    waiting: list[Path] = []

    class Worker:
        def __init__(self, queues, connection) -> None:
            pass

        def work(self, with_scheduler: bool) -> None:
            waiting.append(data_dir)

    monkeypatch.setattr(main, "SimpleWorker", Worker)
    return {"settings": config.load_config(config_dir), "data_dir": data_dir, "waiting": waiting}


def _old_db(path: Path, ddl: str = OLD_RUN, row: str = OLD_ROW) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(ddl))
        conn.execute(text(row))
    engine.dispose()


def _sync(
    settings: config.PipelineConfig, history: History, run_once=sync.run_once
) -> sync.SyncResult:
    prs = [
        OpenPR(number=1, head_sha=HEAD, base_sha="b" * 40, head_ref="f", draft=False),
        OpenPR(number=2, head_sha=NEW_HEAD, base_sha="b" * 40, head_ref="g", draft=False),
    ]
    return run_once(
        settings, fakeredis.FakeStrictRedis(), history, list_prs=lambda repo: prs,
        head_time=lambda repo, sha: "2026-11-20T00:00:00Z",
        list_comments=lambda repo, n, since: [],
    )  # fmt: skip


def _columns(path: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in inspect(engine).get_columns("run")}
    finally:
        engine.dispose()


def test_sync_reads_the_old_db_after_the_worker_has_started(deploy) -> None:
    """배포 순서 그대로: 워커가 뜨고 작업이 오기 전에 동기화가 돈다. 옛 행은 그대로 읽힌다."""
    settings = deploy["settings"]
    _old_db(settings.db_path)

    assert main.main() == 0
    assert deploy["waiting"] == [deploy["data_dir"]]  # 작업은 하나도 처리하지 않았다

    result = _sync(settings, History(settings.db_path))
    # PR 1의 done은 파이프라인 커밋을 적기 전의 기록이라 지금 코드로 다시 검사한다
    assert result.enqueued == [("o/r", 1, "new_pipeline"), ("o/r", 2, "no_record")]


def test_sync_started_before_the_worker_recovers_on_the_next_cycle(deploy, caplog) -> None:
    """honcho는 api와 워커를 같이 띄운다. api의 첫 동기화가 먼저 옛 DB를 읽으면 그 주기는
    실패로 남고, 루프는 살아서 워커가 스키마를 맞춘 다음 주기에 같은 History로 읽는다."""
    settings = deploy["settings"].model_copy(update={"sync": config.SyncConfig(interval_minutes=0)})
    _old_db(settings.db_path)
    cycles: list[str] = []
    results: list[sync.SyncResult] = []

    def cycle(settings, redis, history) -> sync.SyncResult:
        cycles.append("sync")
        if len(cycles) == 3:
            raise asyncio.CancelledError
        if len(cycles) == 2:
            assert main.main() == 0  # 워커는 api의 첫 주기 뒤에 뜬다
        results.append(_sync(settings, history))
        return results[-1]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(api.sync, "run_once", cycle)
    caplog.set_level(logging.ERROR, logger=api.__name__)
    try:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(api.sync_loop(settings, fakeredis.FakeStrictRedis()))
    finally:
        monkeypatch.undo()

    (failed,) = [r for r in caplog.records if r.name == api.__name__]
    assert failed.getMessage() == "동기화 실패"
    assert isinstance(failed.exc_info[1], OperationalError)
    assert [r.enqueued for r in results] == [[("o/r", 1, "new_pipeline"), ("o/r", 2, "no_record")]]


def test_start_up_creates_the_db_on_a_fresh_machine_and_is_idempotent(deploy) -> None:
    db_path = deploy["settings"].db_path
    assert not deploy["data_dir"].exists()

    assert main.main() == 0
    columns = _columns(db_path)
    assert "author_association" in columns
    assert main.main() == 0  # 이미 맞춘 DB를 다시 열어도 그대로다
    assert _columns(db_path) == columns
    assert deploy["waiting"] == [deploy["data_dir"]] * 2


def test_start_up_keeps_existing_rows(deploy) -> None:
    db_path = deploy["settings"].db_path
    _old_db(db_path)
    assert main.main() == 0
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT head_sha, state, author_association FROM run")).one()
    engine.dispose()
    assert tuple(row) == (HEAD, "done", None)


def test_unwritable_db_path_stops_the_worker_loudly(deploy, caplog) -> None:
    locked = deploy["data_dir"].parent / "locked"
    locked.mkdir(mode=0o500)
    data_dir = locked / "data"
    (config.config_dir() / "config.yaml").write_text(f"repos: [o/r]\ndata_dir: {data_dir}\n")
    try:
        assert main.main() == 1
    finally:
        locked.chmod(0o700)
    _assert_refused(deploy, caplog, data_dir / "pipeline.db")


def test_column_the_upgrader_refuses_stops_the_worker_loudly(deploy, caplog) -> None:
    """NULL 불가 열이 빠진 옛 테이블은 더할 수 없다. 반쯤 맞춘 DB로 작업을 받지 않는다."""
    db_path = deploy["settings"].db_path
    row = OLD_ROW.replace("kind, ", "").replace("'update', ", "")
    _old_db(db_path, OLD_RUN.replace("kind VARCHAR NOT NULL, ", ""), row)
    assert main.main() == 1
    _assert_refused(deploy, caplog, db_path)
    assert "run.kind" in caplog.text


def _assert_refused(deploy, caplog, db_path: Path) -> None:
    assert deploy["waiting"] == []  # 큐를 기다리지 않았다
    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert record.getMessage() == f"DB 스키마를 맞추지 못해 워커를 시작하지 않는다: {db_path}"
    assert record.exc_info is not None
