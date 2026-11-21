"""scripts/의 시작 스크립트와 launchd 등록·제거. 이 기계의 launchd와 ~/Library에는 손대지 않는다.

임시 HOME과 임시 체크아웃에 스크립트를 복사하고, launchctl·pgrep은 PATH 앞의 가짜로 바꾼다.
"""

import ast
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[1]
LABEL: Final = "com.pr-proof.server"
TEMPLATE: Final = REPO / "ops" / "launchd" / f"{LABEL}.plist"
needs_plutil = pytest.mark.skipif(shutil.which("plutil") is None, reason="macOS의 plutil이 없다")


def _stub(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture
def box(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path.resolve()
    checkout, home, stubs = root / "checkout", root / "home", root / "stubs"
    shutil.copytree(REPO / "scripts", checkout / "scripts")
    shutil.copytree(REPO / "ops", checkout / "ops")
    home.mkdir()
    log = root / "launchctl.log"
    # 가짜가 빠지면 /bin/launchctl이 불리므로 등록·제거 테스트는 모두 이 둘을 앞에 둔다
    _stub(
        stubs / "launchctl",
        f'echo "$*" >> {log}\n[ "$1" = print ] && exit ${{PRINT_RC:-1}}\nexit 0',
    )
    _stub(stubs / "pgrep", "exit ${PGREP_RC:-1}")
    return {"checkout": checkout, "home": home, "stubs": stubs, "log": log, "root": root}


def _run(box, script: str, **env: str) -> subprocess.CompletedProcess[str]:
    base = {"HOME": str(box["home"]), "PATH": f"{box['stubs']}:/usr/bin:/bin"}
    return subprocess.run(
        ["/bin/bash", str(box["checkout"] / "scripts" / script)],
        env=base | env, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip


def _venv(box) -> Path:
    venv = box["checkout"] / ".venv" / "bin"
    _stub(venv / "honcho", 'pwd\necho "args:$*"\n/usr/bin/env | /usr/bin/sort')
    _stub(venv / "python", "exit 0")
    _stub(venv / "uvicorn", "exit 0")
    return venv


def _tools(box, names=("gh", "claude", "docker", "git")) -> Path:
    system = box["root"] / "system"
    system.mkdir(exist_ok=True)
    for name in names:
        _stub(system / name, "exit 0")
    return system


def test_start_without_a_venv_says_how_to_make_one(box) -> None:
    result = _run(box, "start.sh", PR_PROOF_SYSTEM_PATH=str(_tools(box)))
    assert result.returncode == 1
    assert f"{box['checkout']}/.venv/bin/honcho 이 없다" in result.stderr
    assert "python3 -m venv .venv" in result.stderr


def test_start_names_the_missing_tool(box) -> None:
    _venv(box)
    system = _tools(box, names=("gh", "docker", "git"))
    result = _run(box, "start.sh", PR_PROOF_SYSTEM_PATH=str(system))
    assert result.returncode == 1
    assert result.stderr.startswith("start.sh: claude 을 찾지 못했다 (PATH=")


def test_start_execs_honcho_in_the_checkout_with_a_minimal_environment(box) -> None:
    venv = _venv(box)
    system = _tools(box)
    result = _run(
        box, "start.sh", PR_PROOF_SYSTEM_PATH=str(system), USER="me", GH_TOKEN="secret",
        AWS_SECRET_ACCESS_KEY="secret",
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    cwd, args, *env_lines = result.stdout.splitlines()
    assert (cwd, args) == (str(box["checkout"]), "args:start")
    env = dict(line.split("=", 1) for line in env_lines)
    for own in ("PWD", "SHLVL", "_"):  # 가짜 honcho의 sh가 스스로 정한다
        env.pop(own, None)
    assert env == {
        "HOME": str(box["home"]),
        "USER": "me",
        "PATH": f"{venv}:{box['home']}/.local/bin:{system}",
    }


def test_start_finds_claude_in_the_users_local_bin(box) -> None:
    """claude의 설치 프로그램은 ~/.local/bin에 둔다. launchd의 PATH에는 없다."""
    _venv(box)
    _stub(box["home"] / ".local" / "bin" / "claude", "exit 0")
    system = _tools(box, names=("gh", "docker", "git"))
    assert _run(box, "start.sh", PR_PROOF_SYSTEM_PATH=str(system)).returncode == 0


@needs_plutil
def test_template_is_a_valid_plist() -> None:
    subprocess.run(["plutil", "-lint", str(TEMPLATE)], check=True, capture_output=True)


@needs_plutil
def test_install_writes_absolute_paths_and_bootstraps_the_user_agent(box) -> None:
    result = _run(box, "install-launchd.sh")
    assert result.returncode == 0, result.stderr
    plist = box["home"] / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True)
    logs = box["home"] / "Library" / "Logs" / "pr-proof"
    assert logs.is_dir()  # launchd는 로그 폴더를 만들지 않는다
    assert plistlib.loads(plist.read_bytes()) == {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", f"{box['checkout']}/scripts/start.sh"],
        "WorkingDirectory": str(box["checkout"]),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 60,
        "StandardOutPath": f"{logs}/server.log",
        "StandardErrorPath": f"{logs}/server.log",
    }
    domain = f"gui/{os.getuid()}"
    assert box["log"].read_text().splitlines() == [
        f"print {domain}/{LABEL}",
        f"bootstrap {domain} {plist}",
    ]
    assert f"launchctl print {domain}/{LABEL}" in result.stdout


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PGREP_RC": "0"}, "launchd 밖에서 pr-proof 서버가 돌고 있다"),
        ({"PRINT_RC": "0"}, "이미 등록되어 있다"),
    ],
)
def test_install_refuses_when_a_server_is_already_running(box, env, message) -> None:
    result = _run(box, "install-launchd.sh", **env)
    assert result.returncode == 1
    assert message in result.stderr
    assert not (box["home"] / "Library" / "LaunchAgents").exists()
    assert "bootstrap" not in box["log"].read_text()


def test_uninstall_boots_out_and_removes_the_plist(box) -> None:
    plist = box["home"] / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("x")
    result = _run(box, "uninstall-launchd.sh", PRINT_RC="0")
    assert result.returncode == 0, result.stderr
    assert not plist.exists()
    assert f"bootout gui/{os.getuid()}/{LABEL}" in box["log"].read_text()


def test_uninstall_without_an_agent_only_removes_the_file(box) -> None:
    result = _run(box, "uninstall-launchd.sh")
    assert result.returncode == 0, result.stderr
    assert "bootout" not in box["log"].read_text()


def _procfile() -> dict[str, str]:
    lines = (REPO / "Procfile").read_text().splitlines()
    return dict(line.split(": ", 1) for line in lines if line.strip())


def test_procfile_entry_points_exist() -> None:
    """Procfile이 가리키는 모듈과 앱 객체가 저장소에 있다. 폴더를 옮기면 문자열이라 조용히
    깨진다."""
    procs = _procfile()
    worker = re.fullmatch(r"python -m ([\w.]+)", procs["worker"])
    api = re.match(r"uvicorn ([\w.]+):(\w+) ", procs["api"])
    assert worker and api
    assert REPO.joinpath(*worker.group(1).split(".")).with_suffix(".py").is_file()
    tree = ast.parse(REPO.joinpath(*api.group(1).split(".")).with_suffix(".py").read_text())
    assigned = {t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                if isinstance(t, ast.Name)}  # fmt: skip
    assert api.group(2) in assigned


def test_install_detects_the_procfile_processes() -> None:
    """install-launchd.sh가 launchd 밖에서 도는 서버를 찾는 pgrep 패턴이 Procfile의 두 명령에
    맞는다."""
    pattern = re.search(
        r"pgrep -f '([^']+)'", (REPO / "scripts" / "install-launchd.sh").read_text()
    )
    assert pattern
    for name, command in _procfile().items():
        assert re.search(pattern.group(1), command.split(" --")[0]), name
