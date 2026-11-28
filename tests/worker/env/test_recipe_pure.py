"""app/worker/env/recipe_pure.py 단위 테스트: 경계의 검사와 층의 키."""

import copy

import pytest

from app.worker.env.recipe_pure import (
    MAX_COMMANDS,
    MAX_OVERLAY_BYTES,
    EnvRecipe,
    GitFetch,
    Overlay,
    Step,
    TestStep,
    deps_key,
    recipe_json,
    recipe_key,
    system_key,
    validate,
)

REV = "ba33ef1a2fb3c6519e819cd767bb8fd1677f1323"
VALID = {
    "system": {
        "image": "debian:bookworm-slim",
        "commands": ["apt-get update", "apt-get install -y git cmake"],
        "fetches": [{"url": "https://github.com/microsoft/vcpkg.git", "rev": REV, "dest": "vcpkg"}],
    },
    "deps": {"inputs": ["vcpkg.json"], "commands": ["/opt/env/vcpkg/vcpkg install"]},
    "overlays": [{"path": "ports/x/portfile.cmake", "content": "# x\n", "why": "Linux용"}],
    "env": {"VCPKG_ROOT": "/opt/env/vcpkg"},
    "build": [{"name": "configure", "command": "cmake --preset debug"}],
    "test": {
        "name": "test",
        "command": "ctest --output-junit /work/.pipeline-out/junit.xml",
        "report": "junit",
        "report_path": ".pipeline-out/junit.xml",
    },
    "outputs": ["build/"],
}


def _with(path: str, value: object) -> dict:
    """VALID에서 점으로 이은 path의 값만 바꾼 사본. value가 KeyError면 그 칸을 지운다."""
    data = copy.deepcopy(VALID)
    *parents, last = path.split(".")
    node = data
    for key in parents:
        node = node[int(key)] if isinstance(node, list) else node[key]
    if value is KeyError:
        del node[last]
    elif isinstance(node, list):
        node[int(last)] = value
    else:
        node[last] = value
    return data


def _recipe(data: dict | None = None) -> EnvRecipe:
    recipe = validate(VALID if data is None else data)
    assert isinstance(recipe, EnvRecipe), recipe
    return recipe


def test_valid_recipe_becomes_typed_values() -> None:
    recipe = _recipe()
    assert recipe.system.fetches == (
        GitFetch("https://github.com/microsoft/vcpkg.git", REV, "vcpkg"),
    )
    assert recipe.overlays == (Overlay("ports/x/portfile.cmake", "# x\n", "Linux용"),)
    assert recipe.env == (("VCPKG_ROOT", "/opt/env/vcpkg"),)
    assert recipe.build == (Step("configure", "cmake --preset debug"),)
    assert recipe.test == TestStep(
        "test", VALID["test"]["command"], "junit", ".pipeline-out/junit.xml"
    )
    assert recipe.outputs == ("build",)


def test_minimal_recipe_needs_only_the_image() -> None:
    recipe = _recipe({"system": {"image": "debian:bookworm-slim"}})
    assert recipe.deps is None and recipe.test is None and recipe.build == ()


def test_recipe_json_round_trips() -> None:
    recipe = _recipe()
    assert validate(recipe_json(recipe)) == recipe


@pytest.mark.parametrize(
    "image",
    [
        "debian:bookworm-slim",
        "ubuntu",
        "ghcr.io/owner/image:1.2",
        "localhost:5000/a/b",
        f"debian@sha256:{'a' * 64}",
    ],
)
def test_image_references_are_accepted(image: str) -> None:
    assert isinstance(validate(_with("system.image", image)), EnvRecipe)


@pytest.mark.parametrize(
    ("path", "value", "where"),
    [
        ("system.image", "Debian:Latest", "system.image"),
        ("system.image", "--privileged", "system.image"),
        ("system.image", "debian bookworm", "system.image"),
        ("system.image", "", "system.image"),
        ("system.fetches.0.url", "http://github.com/a.git", "system.fetches[0].url"),
        ("system.fetches.0.url", "https://user:pw@github.com/a.git", "system.fetches[0].url"),
        ("system.fetches.0.url", "file:///etc/passwd", "system.fetches[0].url"),
        ("system.fetches.0.rev", "main", "system.fetches[0].rev"),
        ("system.fetches.0.rev", REV[:12], "system.fetches[0].rev"),
        ("system.fetches.0.rev", REV.upper(), "system.fetches[0].rev"),
        ("system.fetches.0.dest", "/opt/vcpkg", "system.fetches[0].dest"),
        ("system.fetches.0.dest", "src/vcpkg", "system.fetches[0].dest"),
        ("system.fetches.0.dest", "overlay", "system.fetches[0].dest"),
        ("system.commands", "apt-get update", "system.commands"),
        ("system.commands", ["ok"] * (MAX_COMMANDS + 1), "system.commands"),
        ("deps.inputs", ["../secret"], "deps.inputs"),
        ("deps.inputs", ["/etc/passwd"], "deps.inputs"),
        ("deps.inputs", ["-rf"], "deps.inputs"),
        ("deps.inputs", ["a//b"], "deps.inputs"),
        ("deps.inputs", ["a/./b"], "deps.inputs"),
        ("deps.inputs", ["a", "a"], "deps.inputs"),
        ("overlays.0.path", "../x", "overlays[0].path"),
        ("overlays.0.content", "x" * (MAX_OVERLAY_BYTES + 1), "overlays[0].content"),
        ("overlays.0.why", "", "overlays[0].why"),
        ("env", {"1BAD": "x"}, "env"),
        ("env", {"A-B": "x"}, "env"),
        ("env", ["A=1"], "env"),
        ("build.0.name", "Configure", "build[0].name"),
        ("build.0.name", "a" * 33, "build[0].name"),
        ("build.0.name", "-x", "build[0].name"),
        ("build.0.command", "", "build[0].command"),
        ("test.name", "configure", "build/test"),
        ("test.report", "tap", "test.report"),
        ("test.report_path", "junit.xml", "test.report_path"),
        ("test.report_path", ".pipeline-out/a/junit.xml", "test.report_path"),
        ("test.report_path", ".pipeline-out/../x", "test.report_path"),
        ("outputs", ["../build"], "outputs"),
        ("extra", 1, "처방"),
        ("system.extra", 1, "system"),
    ],
)
def test_each_rule_rejects_with_the_field_named(path: str, value: object, where: str) -> None:
    problems = validate(_with(path, value))
    assert isinstance(problems, list)
    assert any(p.startswith(f"{where}") for p in problems), problems


def test_overlays_need_a_deps_layer() -> None:
    problems = validate(_with("deps", KeyError))
    assert isinstance(problems, list) and any(p.startswith("overlays:") for p in problems)


def test_exit_report_takes_no_path() -> None:
    exit_step = {"name": "test", "command": "make test", "report": "exit"}
    recipe = _recipe(_with("test", exit_step))
    assert recipe.test == TestStep("test", "make test", "exit", "")
    problems = validate(_with("test", {**exit_step, "report_path": ".pipeline-out/x.xml"}))
    assert isinstance(problems, list) and problems[0].startswith("test.report_path")


def test_not_an_object_is_rejected() -> None:
    assert validate([]) == ["처방: JSON 객체여야 함"]  # type: ignore[arg-type]
    assert validate({}) == ["system: JSON 객체여야 함"]


def test_all_problems_are_reported_at_once() -> None:
    data = _with("system.image", "BAD IMAGE")
    data["build"][0]["name"] = "BAD"
    problems = validate(data)
    assert isinstance(problems, list) and len(problems) == 2


USER = "501:20"
DIGEST = f"debian@sha256:{'a' * 64}"
INPUTS = {"vcpkg.json": b"{}"}


def _keys(recipe: EnvRecipe, *, digest: str = DIGEST, user: str = USER,
          inputs: dict[str, bytes] = INPUTS) -> tuple[str, str]:  # fmt: skip
    system = system_key(recipe, digest, user)
    return system, deps_key(recipe, system, user, inputs)


def test_keys_are_stable() -> None:
    assert _keys(_recipe()) == _keys(_recipe())


@pytest.mark.parametrize(
    ("change", "system_changes"),
    [
        (lambda: _keys(_recipe(), digest=f"debian@sha256:{'b' * 64}"), True),
        (lambda: _keys(_recipe(), user="1000:1000"), True),
        (lambda: _keys(_recipe(_with("system.commands.1", "apt-get install -y git"))), True),
        (lambda: _keys(_recipe(_with("deps.commands.0", "vcpkg install --x"))), False),
        (lambda: _keys(_recipe(_with("overlays.0.content", "# y\n"))), False),
        (lambda: _keys(_recipe(), inputs={"vcpkg.json": b"{ }"}), False),
    ],
    ids=["digest", "uid", "system-command", "deps-command", "overlay", "input-content"],
)
def test_keys_change_when_an_input_changes(change, system_changes: bool) -> None:  # noqa: ANN001
    base_system, base_deps = _keys(_recipe())
    system, deps = change()
    assert (system != base_system) is system_changes
    assert deps != base_deps


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("overlays.0.why", "다른 설명"),
        ("build.0.command", "cmake --preset release"),
        ("env", {"VCPKG_ROOT": "/x"}),
        ("test.command", "ctest"),
    ],
)
def test_layer_keys_ignore_what_layers_do_not_hold(path: str, value: object) -> None:
    changed = _recipe(_with(path, value))
    assert _keys(changed) == _keys(_recipe())
    assert recipe_key(changed) != recipe_key(_recipe())
