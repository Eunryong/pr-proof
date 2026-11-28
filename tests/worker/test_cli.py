from app.worker import cli


def test_reindex_rebuilds_the_vector_store_from_records(monkeypatch, tmp_path, capsys) -> None:
    import pytest

    from ..redis_stand_ins import MemoryKeys

    pytest.importorskip("chromadb")
    from app.worker.store.records_pure import NewRecord
    from app.worker.store.state import Store

    from .store.rag_helpers import embed_encoder

    settings = cli.config.PipelineConfig(
        repos=["o/r"], data_dir=tmp_path, redis_url="redis://127.0.0.1:1/0"
    )
    db = Store(settings.db_path, MemoryKeys())
    db.add_record(NewRecord(repo="o/r", kind="adopted", origin="fix_pr", source_id="a",
                            rule_id="R", rule_text="규칙", file="a.py", line_text="x",
                            context="x", diff="+x"))  # fmt: skip
    assert cli.reindex(settings, None, encoder=embed_encoder()) == 0
    assert "o/r: 기록 1건" in capsys.readouterr().out
    assert (tmp_path / "chroma").is_dir()
    assert all(r.embedded_at is not None for r in db.records("o/r"))


def test_reindex_without_the_model_reports_and_fails(monkeypatch, tmp_path, capsys) -> None:
    from app.worker.store import memory

    settings = cli.config.PipelineConfig(
        repos=["o/r"], data_dir=tmp_path, redis_url="redis://127.0.0.1:1/0"
    )

    def fail(*a, **k):
        raise memory.SearchUnavailable("받지 못함")

    monkeypatch.setattr(memory, "ensure_model", fail)
    assert cli.reindex(settings, None) == 1
    assert "reindex하지 못함" in capsys.readouterr().err


def test_env_try_prints_recipe_problems_one_per_line_and_exits_2(tmp_path, capsys) -> None:
    recipe = tmp_path / "recipe.json"
    recipe.write_text(
        '{"system": {"image": "BAD IMAGE"}, "build": [{"name": "X", "command": "x"}]}'
    )
    assert cli.main(["env-try", str(recipe), str(tmp_path)]) == 2
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "처방에서 고칠 곳:"
    assert [line.split(":")[0] for line in lines[1:]] == ["- system.image", "- build[0].name"]


def test_env_try_rejects_unreadable_json_and_wrong_arguments(tmp_path, capsys) -> None:
    import pytest

    recipe = tmp_path / "recipe.json"
    recipe.write_text("{not json")
    assert cli.main(["env-try", str(recipe), str(tmp_path)]) == 2
    assert "처방을 읽지 못함" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["env-try", str(recipe)])


def test_env_plan_takes_exactly_one_repository_folder(tmp_path) -> None:
    import pytest

    for argv in (["env-plan"], ["env-plan", str(tmp_path), str(tmp_path)]):
        with pytest.raises(SystemExit):
            cli.main(argv)
