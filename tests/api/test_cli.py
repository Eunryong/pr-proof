from api import cli
from api.sync import SyncResult


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
