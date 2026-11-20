from app import cli
from app.controllers.sync import SyncResult


def _patch(monkeypatch, tmp_path, result: SyncResult) -> None:
    # 운영과 같은 ~/.cache/pipeline·Redis를 가리키지 않게 한다 (지금은 둘 다 연결 전에 끝나지만)
    settings = cli.config.PipelineConfig(
        repos=["o/r"], data_dir=tmp_path, redis_url="redis://127.0.0.1:1/0"
    )
    monkeypatch.setattr(cli.config, "load_config", lambda: settings)
    monkeypatch.setattr(cli.sync, "run_once", lambda *a, **k: result)


def test_sync_prints_enqueued(monkeypatch, tmp_path, capsys) -> None:
    _patch(monkeypatch, tmp_path, SyncResult(enqueued=[("o/r", 1, "no_record")]))
    assert cli.main(["sync"]) == 0
    assert "등록: o/r#1 (no_record)" in capsys.readouterr().out


def test_sync_failure_exit_code(monkeypatch, tmp_path, capsys) -> None:
    _patch(monkeypatch, tmp_path, SyncResult(failed_repos=["o/r"]))
    assert cli.main(["sync"]) == 1
    assert "조회 실패: o/r" in capsys.readouterr().err


def test_reindex_rebuilds_the_vector_store_from_records(monkeypatch, tmp_path, capsys) -> None:
    import fakeredis
    import pytest

    pytest.importorskip("chromadb")
    from app.models.records import NewRecord
    from app.worker.state import Store

    from .worker.rag_helpers import FakeEncoder

    settings = cli.config.PipelineConfig(
        repos=["o/r"], data_dir=tmp_path, redis_url="redis://127.0.0.1:1/0"
    )
    db = Store(settings.db_path, fakeredis.FakeRedis())
    db.add_record(NewRecord(repo="o/r", kind="adopted", origin="fix_pr", source_id="a",
                            rule_id="R", rule_text="규칙", file="a.py", line_text="x",
                            context="x", diff="+x"))  # fmt: skip
    assert cli.reindex(settings, None, encoder=FakeEncoder()) == 0
    assert "o/r: 기록 1건" in capsys.readouterr().out
    assert (tmp_path / "chroma").is_dir()
    assert all(r.embedded_at is not None for r in db.records("o/r"))


def test_reindex_without_the_model_reports_and_fails(monkeypatch, tmp_path, capsys) -> None:
    from app.worker import memory

    settings = cli.config.PipelineConfig(
        repos=["o/r"], data_dir=tmp_path, redis_url="redis://127.0.0.1:1/0"
    )

    def fail(*a, **k):
        raise memory.SearchUnavailable("받지 못함")

    monkeypatch.setattr(memory, "ensure_model", fail)
    assert cli.reindex(settings, None) == 1
    assert "reindex하지 못함" in capsys.readouterr().err
