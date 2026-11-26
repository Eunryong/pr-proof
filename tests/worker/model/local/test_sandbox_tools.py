"""`app/worker/model/local/sandbox_tools.py`의 경로 격리 및 파일 도구 단위 테스트."""

from pathlib import Path

from app.worker.model.local.sandbox_tools import SandboxTools


def test_read_grep_glob_stay_inside_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("CANARY-SECRET\n")

    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("X = 1\nY = 2\n")
    (root / "link.txt").symlink_to(secret)
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("git-secret\n")

    tools = SandboxTools(root, allow_write=False)

    # 정상 읽기
    out = tools.execute("read_file", {"path": "src/app.py", "offset": 2, "limit": 1})
    assert out == "2: Y = 2"
    assert "src/app.py" in tools.reads

    # 없는 파일은 reads에 남기지 않는다
    assert "읽을 수 없음" in tools.execute("read_file", {"path": "missing.txt"})
    assert "missing.txt" not in tools.reads

    # 폴더 밖 절대 경로, .., 심볼릭 링크, .git 모두 거부
    for bad in (str(secret), "../outside/secret.txt", "link.txt", ".git/config"):
        res = tools.execute("read_file", {"path": bad})
        assert "거부됨" in res and "CANARY-SECRET" not in res and "git-secret" not in res
    assert len(tools.denials) == 4

    # grep은 일반 파일만 보고 심볼릭 링크와 .git은 건너뛴다
    grep_out = tools.execute("grep", {"pattern": "X = 1|CANARY|git-secret"})
    assert "src/app.py:1: X = 1" in grep_out
    assert "CANARY" not in grep_out and "git-secret" not in grep_out
    assert "." in tools.reads

    # glob은 일반 파일만 나열하고 탈출 패턴은 거부
    assert tools.execute("glob", {"pattern": "**/*.py"}) == "src/app.py"
    assert "거부됨" in tools.execute("glob", {"pattern": "../**/*.txt"})

    # 읽기 전용 모드에서는 쓰기 도구와 모르는 도구 거부
    assert "거부됨" in tools.execute("write_file", {"path": "src/new.py", "content": "1"})
    assert "거부됨" in tools.execute("bash", {"command": "id"})


def test_write_and_edit_confine_changes_to_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("KEEP\n")

    root = tmp_path / "work"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("a = 1\nb = 1\n")
    (root / "link.txt").symlink_to(secret)

    tools = SandboxTools(root, allow_write=True)

    # 정상 쓰기·수정
    assert "작성함" in tools.execute("write_file", {"path": "src/new.py", "content": "Z = 3\n"})
    assert (root / "src" / "new.py").read_text() == "Z = 3\n"

    # 중복 일치는 replace_all 없이 오류
    assert "오류" in tools.execute(
        "edit_file", {"path": "src/app.py", "old_string": "= 1", "new_string": "= 2"}
    )
    assert "수정함" in tools.execute(
        "edit_file",
        {"path": "src/app.py", "old_string": "a = 1", "new_string": "a = 9"},
    )
    assert (root / "src" / "app.py").read_text() == "a = 9\nb = 1\n"

    # 탈출·심볼릭 링크 쓰기 거부
    for bad in ("../escaped.txt", str(outside / "x.txt"), "link.txt", ".git/HEAD"):
        assert "거부됨" in tools.execute("write_file", {"path": bad, "content": "bad"})
    assert secret.read_text() == "KEEP\n"
    assert not (tmp_path / "escaped.txt").exists()
