"""환경 처방 통합 테스트. 실제 Docker로 작은 처방의 층을 굽고 작은 저장소에서 단계를 돌린다.

시작 이미지는 debian:bookworm-slim이고 큰 것을 받지 않는다. 결과 파일은 printf로 쓴다. 시스템 층의
`apt-get update`는 굽기가 프록시로 밖에 닿는지 본다(굽는 컨테이너는 이름을 직접 풀지 못한다).
"""

import os
import subprocess
from pathlib import Path

import pytest

from app.worker.env import BakeFailed, EnvRecipe, Layers, Limits, ensure_layers, open_environment
from app.worker.env import validate as validate_recipe
from tests.sandbox_names import TEST_NAMES

pytestmark = pytest.mark.docker

LIMITS = Limits(memory_gb=1, cpus=1, install_timeout_s=300, step_timeout_s=60)
JUNIT = ".pipeline-out/junit.xml"
PASSING = (
    '<testsuite><testcase classname="t" name="one"/><testcase classname="t" name="two"/>'
    "</testsuite>"
)
FAILING = '<testsuite><testcase classname="t" name="one"><failure/></testcase></testsuite>'


def _recipe(*, test_command: str, build: str = "true", report: str = "junit", **extra) -> EnvRecipe:
    data = {
        "system": {
            "image": "debian:bookworm-slim",
            "commands": ["apt-get update -qq && date > /opt/env/reached"],
        },
        "deps": {
            "inputs": ["dep.txt"],
            "commands": ["cat /opt/env/src/dep.txt /opt/env/overlay/x/o.txt > /opt/env/both.txt"],
        },
        "overlays": [{"path": "x/o.txt", "content": "from-overlay\n", "why": "시험"}],
        "env": {"GREETING": "hi"},
        "build": [{"name": "build", "command": build}],
        "test": {
            "name": "test",
            "command": test_command,
            "report": report,
            "report_path": JUNIT if report == "junit" else "",
        },
        **extra,
    }
    recipe = validate_recipe(data)
    assert isinstance(recipe, EnvRecipe), recipe
    return recipe


def _write_junit(xml: str, exit_code: int = 0) -> str:
    return f"printf '%s' '{xml}' > {JUNIT}; exit {exit_code}"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "dep.txt").write_text("from-repo\n")
    return tmp_path


def _layers(recipe: EnvRecipe, repo: Path) -> Layers:
    layers = ensure_layers(recipe, repo, LIMITS, names=TEST_NAMES)
    assert isinstance(layers, Layers), layers
    return layers


def _run(recipe: EnvRecipe, repo: Path):  # noqa: ANN202
    with open_environment(_layers(recipe, repo), recipe, repo, LIMITS, names=TEST_NAMES) as env:
        return env.run()


def test_passing_tests_build_in_the_repository_with_layers_and_overlays(repo: Path) -> None:
    build = "cat /opt/env/both.txt > built.txt && echo $GREETING >> built.txt && id -u >> built.txt"
    run = _run(_recipe(build=build, test_command=_write_junit(PASSING)), repo)
    assert run.status == "passed", run.output
    assert (run.report.total, run.report.passed) == (2, 2)
    assert run.report_path == repo / JUNIT
    assert [s.name for s in run.build] == ["build"] and run.build[0].seconds > 0
    assert (repo / "built.txt").read_text() == f"from-repo\nfrom-overlay\nhi\n{os.getuid()}\n"


def test_failing_test_is_reported_with_its_name(repo: Path) -> None:
    run = _run(_recipe(test_command=_write_junit(FAILING, 8)), repo)
    assert run.status == "failed"
    assert run.report.failed_tests == ("t.one",)


def test_failing_build_step_stops_before_the_test(repo: Path) -> None:
    run = _run(_recipe(build="echo broken; exit 3", test_command=_write_junit(PASSING)), repo)
    assert run.status == "build_failed" and run.test is None
    assert run.build[0].exit_code == 3 and "broken" in run.output


def test_leftover_report_is_cleared_before_the_run(repo: Path) -> None:
    (repo / ".pipeline-out").mkdir()
    (repo / JUNIT).write_text(PASSING)
    run = _run(_recipe(test_command="exit 0"), repo)
    assert run.status == "unrunnable" and "종료 코드 0" in run.unrunnable_reason


def test_run_stage_has_no_network_but_the_bake_did(repo: Path) -> None:
    recipe = _recipe(
        test_command="test -s /opt/env/reached && getent hosts github.com", report="exit"
    )
    run = _run(recipe, repo)
    assert run.status == "failed" and run.test.exit_code == 2


def test_symlinked_out_folder_is_not_followed(repo: Path, tmp_path_factory) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    (elsewhere / "junit.xml").write_text(PASSING)
    test = f"rm -rf .pipeline-out && ln -s {elsewhere} .pipeline-out"
    run = _run(_recipe(test_command=test), repo)
    assert run.status == "unrunnable"


def test_second_bake_reuses_both_layers(repo: Path) -> None:
    recipe = _recipe(test_command="true")
    first = _layers(recipe, repo)
    second = _layers(recipe, repo)
    assert [layer.image for layer in second.layers] == [layer.image for layer in first.layers]
    assert all(layer.reused for layer in second.layers)
    labels = subprocess.run(
        ["docker", "image", "inspect", second.top, "--format", "{{json .Config.Labels}}"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert '"pr-proof.env":"1"' in labels


def test_changed_input_file_bakes_a_new_deps_layer(repo: Path) -> None:
    recipe = _recipe(test_command="true")
    first = _layers(recipe, repo)
    (repo / "dep.txt").write_text(f"changed {os.getpid()} {repo.name}\n")
    second = _layers(recipe, repo)
    assert second.layers[0].image == first.layers[0].image
    assert second.layers[1].image != first.layers[1].image and not second.layers[1].reused


def test_failing_system_command_is_a_recipe_error_and_leaves_no_layer(repo: Path) -> None:
    data = {
        "system": {"image": "debian:bookworm-slim", "commands": [f"echo nope-{repo.name}; exit 3"]}
    }
    recipe = validate_recipe(data)
    failed = ensure_layers(recipe, repo, LIMITS, names=TEST_NAMES)
    assert isinstance(failed, BakeFailed)
    assert (failed.layer, failed.step, failed.exit_code) == ("system", "system.commands[0]", 3)
    assert f"nope-{repo.name}" in failed.output_tail
    leftovers = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={TEST_NAMES.container_prefix}-bake"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert leftovers == ""


def test_fetch_without_git_tells_the_recipe_to_install_it(repo: Path) -> None:
    fetch = {"url": "https://github.com/microsoft/vcpkg.git", "rev": "0" * 40, "dest": "vcpkg"}
    recipe = validate_recipe({"system": {"image": "debian:bookworm-slim", "fetches": [fetch]}})
    failed = ensure_layers(recipe, repo, LIMITS, names=TEST_NAMES)
    assert isinstance(failed, BakeFailed) and failed.step == "system.fetches[0]"
    assert failed.exit_code == 127 and "git이 없음" in failed.output_tail


def test_missing_start_image_is_a_recipe_error(repo: Path) -> None:
    recipe = validate_recipe({"system": {"image": "pr-proof-test/no-such-image:never"}})
    failed = ensure_layers(recipe, repo, LIMITS, names=TEST_NAMES)
    assert isinstance(failed, BakeFailed) and failed.step.startswith("system.image")
