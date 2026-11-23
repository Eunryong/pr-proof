"""`pr-proof start`가 honcho에 넘기는 것. honcho로 바꿔 타는 exec는 받는 인자로 바꿔 끼운다."""

import os
import stat
from pathlib import Path
from typing import Final

import pytest

from app.setup import files, start

SMEE: Final = "https://smee.io/FAKEchannel123"


def _stub(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    config_dir, venv, system = tmp_path / "config", tmp_path / "venv" / "bin", tmp_path / "system"
    config_dir.mkdir()
    (config_dir / files.CONFIG_FILE).write_text("repos: [octocat/hello-world]\n")
    _stub(venv / "honcho")
    for tool in start.TOOLS:
        _stub(system / tool)
    return {"config": config_dir, "venv": venv, "system": system, "runtime": tmp_path / "runtime"}


def _start(box, *, node="/opt/node/bin/node", smee_js="/pkg/smee.js", command=None):
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    lines: list[str] = []
    environ = {
        "HOME": "/home/u", "PATH": "/usr/bin:/bin", "PR_PROOF_SYSTEM_PATH": str(box["system"]),
        "PR_PROOF_HOME": str(box["runtime"]), "GH_TOKEN": "ambient", "SECRET_THING": "x",
    }  # fmt: skip
    if command is not None:
        environ[files.COMMAND_ENV] = command
    cwd = os.getcwd()
    try:
        code = start.start(
            node, smee_js, directory=box["config"], environ=environ, venv_bin=box["venv"],
            exec_=lambda path, args, env: calls.append((path, args, dict(env))), out=lines.append,
        )  # fmt: skip
    finally:
        os.chdir(cwd)
    return code, calls, lines


def test_without_smee_url_runs_api_and_worker_only(box) -> None:
    code, calls, _ = _start(box)
    assert code == 1  # exec가 돌아왔다(가짜라서)
    [(path, args, env)] = calls
    assert path == str(box["venv"] / "honcho")
    procfile = Path(args[args.index("-f") + 1])
    text = procfile.read_text()
    assert "api: uvicorn" in text and "worker:" in text and "smee" not in text
    assert args[args.index("-d") + 1] == str(files.PACKAGE_ROOT)
    assert args[args.index("-e") + 1] == str(box["config"] / files.RUN_ENV_FILE)


def test_smee_url_adds_smee_client_aimed_at_the_api(box) -> None:
    files.write_private(box["config"] / files.WEBHOOK_URL_FILE, SMEE)
    _, [(_, args, _)], lines = _start(box)
    procfile = Path(args[args.index("-f") + 1])
    smee = [line for line in procfile.read_text().splitlines() if line.startswith("smee:")]
    assert smee == [
        f"smee: /opt/node/bin/node /pkg/smee.js --url {SMEE} "
        '--target "http://127.0.0.1:${API_PORT:-8765}/webhook/github"'
    ]
    assert stat.S_IMODE(procfile.stat().st_mode) == 0o600  # smee 주소가 들어 있다
    assert "smee에서 받아 오기" in lines[-1]


def test_direct_webhook_url_does_not_start_smee(box) -> None:
    files.write_private(box["config"] / files.WEBHOOK_URL_FILE, "https://hooks.example.com/gh")
    _, [(_, args, _)], _ = _start(box)
    assert "smee" not in Path(args[args.index("-f") + 1]).read_text()


def test_env_is_minimal_and_finds_node(box) -> None:
    _, [(_, _, env)], _ = _start(box)
    assert set(env) == {"HOME", "PATH"}  # GH_TOKEN 같은 이 셸의 다른 변수는 넘기지 않는다
    path = env["PATH"].split(":")
    assert path[:2] == [str(box["venv"]), "/opt/node/bin"]
    assert str(box["system"]) in path


def test_missing_config_says_run_configure(box) -> None:
    (box["config"] / files.CONFIG_FILE).unlink()
    code, calls, lines = _start(box, command="pr-proof")
    assert (code, calls) == (1, [])
    assert "먼저 pr-proof configure를 실행하세요" in lines[-1]
    # 입구가 넘긴 값이 없으면(python -m으로 직접) clone한 폴더라 `npx .`
    assert "먼저 npx . configure를" in _start(box)[2][-1]


def test_missing_tool_is_named(box) -> None:
    (box["system"] / "claude").unlink()
    code, calls, lines = _start(box)
    assert (code, calls) == (1, [])
    assert "claude" in lines[-1]


def test_smee_url_without_client_is_an_error(box) -> None:
    files.write_private(box["config"] / files.WEBHOOK_URL_FILE, SMEE)
    code, calls, lines = _start(box, node=None, smee_js=None)
    assert (code, calls) == (1, [])
    assert "smee-client" in lines[-1]


def test_package_procfile_default_port_is_8765() -> None:
    assert "${API_PORT:-8765}" in (files.PACKAGE_ROOT / "Procfile").read_text()
    assert "API_PORT=8765" in (files.PACKAGE_ROOT / ".env.example").read_text()
