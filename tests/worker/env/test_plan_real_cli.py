"""실제 claude CLI와 Docker로 확인한다: 모델이 작은 C 저장소의 처방을 쓰고 base에서 통과시킨다.

저장소는 zlib을 링크하는 C 프로그램이고, `make test`가 macOS의 경로(/opt/homebrew/bin/python3)로
파이썬 검사를 부른다. 명령은 README에만 적혀 있다. 모델은 시스템 패키지(C 컴파일러, zlib 개발 파일,
python3)를 설치하고 플랫폼의 차이를 맞춰야(make의 변수를 명령줄에서 바꾸는 등) base 실행이 통과한다.
결과만 본다: 처방이 정해지고 base 실행이 passed다. 같은 base의 두 번째 정하기는 저장한 처방을 쓴다.

모델과 Docker를 실제로 부르므로 `PR_PROOF_REAL_AI=1`이고 `pytest -m docker`일 때만 돈다. 호출은
회차마다 1회, 많아야 3회다.
"""

import os
import subprocess
from pathlib import Path
from typing import Final

import pytest

from app.worker.config.repo_config_pure import CallSettings
from app.worker.env.install import Limits
from app.worker.env.plan import EnvReady, plan_environment
from app.worker.git import repo
from app.worker.model.claude.claude_backend import ClaudeCliBackend
from app.worker.store.budget import CallBudget
from app.worker.store.state import Store
from tests.sandbox_names import TEST_NAMES

from ...redis_stand_ins import MemoryKeys

REAL_AI_ENV: Final = "PR_PROOF_REAL_AI"
MODEL: Final = "sonnet"
LIMITS: Final = Limits(memory_gb=2, cpus=2, install_timeout_s=900, step_timeout_s=120)
FILES: Final = {
    "README.md": (
        "# hello-z\n\nzlib으로 문자열을 압축했다 푸는 작은 프로그램.\n\n"
        "## 빌드와 테스트 (macOS)\n\n```\nbrew install zlib python\nmake\nmake test\n```\n"
    ),
    "Makefile": (
        "CC ?= cc\nPYTHON = /opt/homebrew/bin/python3\n\n"
        "hello: hello.c\n\t$(CC) -o hello hello.c -lz\n\n"
        "test: hello\n\t$(PYTHON) test_hello.py\n\n.PHONY: test\n"
    ),
    "hello.c": (
        "#include <stdio.h>\n#include <string.h>\n#include <zlib.h>\n\n"
        "int main(void) {\n"
        '    const char *text = "hello hello hello";\n'
        "    unsigned char packed[128], plain[128];\n"
        "    uLongf packed_len = sizeof packed, plain_len = sizeof plain;\n"
        "    if (compress(packed, &packed_len, (const Bytef *)text, strlen(text) + 1) != Z_OK) "
        "return 1;\n"
        "    if (uncompress(plain, &plain_len, packed, packed_len) != Z_OK) return 1;\n"
        '    printf("%s\\n", plain);\n'
        "    return 0;\n}\n"
    ),
    "test_hello.py": (
        "import subprocess\nimport sys\n\n"
        'out = subprocess.run(["./hello"], capture_output=True, text=True, check=True).stdout\n'
        'if out.strip() != "hello hello hello":\n'
        '    sys.exit(f"unexpected output: {out!r}")\n'
        'print("ok")\n'
    ),
}

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        os.environ.get(REAL_AI_ENV) != "1", reason=f"{REAL_AI_ENV}=1일 때만 실제 모델을 부른다"
    ),
]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip


def test_model_plans_a_c_repository_that_assumes_macos_and_base_passes(tmp_path: Path) -> None:
    clone = tmp_path / "hello-z"
    clone.mkdir()
    _git(clone, "init", "-q", "-b", "main")
    for name, text in FILES.items():
        (clone / name).write_text(text)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "base")
    repo.record_git_dir(clone)
    base = _git(clone, "rev-parse", "HEAD")
    store = Store(tmp_path / "pipeline.db", MemoryKeys())

    def plan():  # noqa: ANN202
        budget = CallBudget(store, "o/hello-z", 0, base, CallSettings())
        return plan_environment(clone, base, store=store, budget=budget, model=MODEL,
                                read_code=ClaudeCliBackend().reading, limits=LIMITS,
                                names=TEST_NAMES)  # fmt: skip

    first = plan()
    assert isinstance(first, EnvReady), first
    assert first.base_run.status == "passed", first
    assert not first.base_run.base_is_red
    again = plan()
    assert isinstance(again, EnvReady) and again.cached and again.plan_id == first.plan_id
