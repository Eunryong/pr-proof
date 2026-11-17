from app import cli
from app.controllers.sync import SyncResult


def _patch(monkeypatch, result: SyncResult) -> None:
    monkeypatch.setattr(cli.config, "load_config", lambda: cli.config.PipelineConfig(repos=["o/r"]))
    monkeypatch.setattr(cli.sync, "run_once", lambda *a, **k: result)


def test_sync_prints_enqueued(monkeypatch, capsys) -> None:
    _patch(monkeypatch, SyncResult(enqueued=[("o/r", 1, "no_record")]))
    assert cli.main(["sync"]) == 0
    assert "등록: o/r#1 (no_record)" in capsys.readouterr().out


def test_sync_failure_exit_code(monkeypatch, capsys) -> None:
    _patch(monkeypatch, SyncResult(failed_repos=["o/r"]))
    assert cli.main(["sync"]) == 1
    assert "조회 실패: o/r" in capsys.readouterr().err
