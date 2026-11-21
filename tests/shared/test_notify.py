"""macOS 알림 띄우기와 30분에 한 번 (6-4). 실제 osascript는 부르지 않는다."""

import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.shared import notify as macos
from app.shared.ops_alert_pure import MARKER_DIR, TEXT, WINDOW_SECONDS, due

# conftest가 테스트마다 macos.show_notification을 가짜로 바꾸기 전, import할 때의 진짜 함수
REAL_SHOW: Final = macos.show_notification
T0: Final = 1_800_000_000.0
MINUTE: Final = 60


class Runner:
    def __init__(self, returncode: int = 0, error: BaseException | None = None) -> None:
        self.returncode, self.error = returncode, error
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((args, kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(args, self.returncode, b"", b"")


@pytest.fixture
def osascript(tmp_path: Path) -> Path:
    path = tmp_path / "osascript"
    path.write_text("")
    return path


def test_text_goes_to_argv_not_into_the_script(osascript: Path, monkeypatch) -> None:
    """글은 AppleScript 소스가 아니라 `on run argv`의 인자다. 셸 없이 인자 목록으로, 최소 환경과
    짧은 시간 제한으로 부른다."""
    monkeypatch.setenv("SECRET_TOKEN", "ghp_x")
    run = Runner()
    text = 'x" & (do shell script "touch /tmp/pwned") & "'
    assert REAL_SHOW(text, platform="darwin", osascript=osascript, run=run) is True
    [(args, kwargs)] = run.calls
    assert args == [
        str(osascript),
        "-e", "on run argv",
        "-e", "display notification (item 1 of argv)",
        "-e", "end run",
        text,
    ]  # fmt: skip
    assert kwargs["timeout"] == macos.TIMEOUT_SECONDS and not kwargs.get("shell")
    assert set(kwargs["env"]) <= {"PATH", "HOME"}


@pytest.mark.parametrize(
    "run",
    [
        Runner(returncode=1),
        Runner(error=subprocess.TimeoutExpired("osascript", macos.TIMEOUT_SECONDS)),
        Runner(error=OSError("실행 못 함")),
    ],
    ids=["non-zero-exit", "timeout", "os-error"],
)
def test_failed_osascript_is_false_without_raising(osascript: Path, run: Runner) -> None:
    assert REAL_SHOW("t", platform="darwin", osascript=osascript, run=run) is False


def test_missing_osascript_or_other_platform_does_nothing(tmp_path: Path, osascript) -> None:
    run = Runner()
    assert REAL_SHOW("t", platform="darwin", osascript=tmp_path / "없음", run=run) is False
    assert REAL_SHOW("t", platform="linux", osascript=osascript, run=run) is False
    assert run.calls == []


@pytest.mark.parametrize(
    ("last", "now", "expected"),
    [
        (None, T0, True),
        (T0, T0 + 29 * MINUTE, False),
        (T0, T0 + WINDOW_SECONDS - 1, False),
        (T0, T0 + 30 * MINUTE, True),
        (T0, T0 - MINUTE, True),  # 시계가 뒤로 갔다. 영영 막히지 않는다
    ],
    ids=["never", "29min", "just-under", "30min", "clock-back"],
)
def test_due(last: float | None, now: float, expected: bool) -> None:
    assert due(last, now) is expected


def _send(tmp_path: Path, now: float, shown: list[str], ok: bool = True, kind="docker") -> str:
    return macos.send(kind, tmp_path, now=now, show=lambda text: shown.append(text) or ok)


def test_same_kind_shows_once_per_window_and_kinds_are_separate(tmp_path: Path) -> None:
    """표시는 파일이라 프로세스를 다시 띄워도(같은 폴더로 다시 불러도) 남는다."""
    shown: list[str] = []
    assert _send(tmp_path, T0, shown) == "shown"
    assert _send(tmp_path, T0 + 29 * MINUTE, shown) == "duplicate"
    assert _send(tmp_path, T0 + 29 * MINUTE, shown, kind="redis") == "shown"
    assert _send(tmp_path, T0 + 30 * MINUTE, shown) == "shown"
    assert shown == [TEXT["docker"], TEXT["redis"], TEXT["docker"]]


def test_failed_show_leaves_no_marker_so_the_next_error_tries_again(tmp_path: Path) -> None:
    shown: list[str] = []
    assert _send(tmp_path, T0, shown, ok=False) == "failed"
    assert not (tmp_path / MARKER_DIR / "docker").exists()
    assert _send(tmp_path, T0 + MINUTE, shown) == "shown"


@pytest.mark.parametrize("mode", ["garbage", "no-permission"])
def test_unreadable_marker_counts_as_never_shown(tmp_path: Path, mode: str) -> None:
    marker = tmp_path / MARKER_DIR / "docker"
    marker.parent.mkdir()
    marker.write_text("어제쯤\n")
    if mode == "no-permission":
        marker.chmod(0)
    shown: list[str] = []
    try:
        assert _send(tmp_path, T0, shown) == "shown"
    finally:
        marker.chmod(0o600)
    assert shown == [TEXT["docker"]]


def test_unwritable_data_dir_shows_nothing_and_does_not_raise(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("")  # data_dir 자리에 파일이 있어 표시 폴더를 만들지 못한다
    shown: list[str] = []
    assert _send(blocked, T0, shown) == "failed"
    assert shown == []
