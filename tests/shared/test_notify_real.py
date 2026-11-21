"""이 Mac에 실제 알림을 한 번 띄운다. 기본 테스트에서는 건너뛴다.

`PR_PROOF_REAL_NOTIFY=1 pytest tests/shared/test_notify_real.py`. 화면 오른쪽 위에 정한 시험 글이
한 줄 떠야 한다 (6-4의 근거).
"""

import os
import sys
from typing import Final

import pytest

from shared import notify as macos

REAL_SHOW: Final = macos.show_notification
TEST_TEXT: Final = "pr-proof: 알림 시험입니다. 할 일은 없습니다."

pytestmark = pytest.mark.skipif(
    os.environ.get("PR_PROOF_REAL_NOTIFY") != "1" or sys.platform != "darwin",
    reason="실제 알림은 macOS에서 PR_PROOF_REAL_NOTIFY=1일 때만 띄운다",
)


def test_one_real_notification() -> None:
    assert REAL_SHOW(TEST_TEXT) is True
