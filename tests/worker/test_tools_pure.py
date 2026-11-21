"""app/worker/tools_pure.py: 도구 목록, ruff 설정과 버전 고정 읽기, 도구 설정의 규칙 묶음."""

import pytest

from app.worker import tools_pure
from app.worker.tools_pure import RuffConfig, RuffSetup, config_rule, pinned_version, valid_codes


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"requirements.txt": "ruff==0.17.4\n"}, ("0.17.4", "requirements.txt")),
        ({"requirements-lint.txt": "ruff[extra] == 0.16.10 ; python_version > '3'\n"},
         ("0.16.10", "requirements-lint.txt")),
        ({"pyproject.toml": '[project]\noptional-dependencies = {dev = ["ruff==0.17.6"]}\n',
          "requirements.txt": "ruff==0.17.4\n"}, ("0.17.6", "pyproject.toml")),
        ({"requirements.txt": "ruff~=0.17\nruffle==1.0\n"}, None),
        ({"pyproject.toml": "[project\n"}, None),
        ({"constraints.txt": "ruff==0.17.4\n"}, None),
    ],
    ids=["requirements", "extras-and-marker", "pyproject-first", "not-exact", "broken", "other"],
)  # fmt: skip
def test_pinned_version(files: dict[str, str], expected) -> None:
    assert pinned_version(files) == expected


def test_pyproject_is_a_ruff_setting_only_with_a_ruff_table() -> None:
    assert tools_pure.ruff_config_name({"pyproject.toml": "[tool.ruff]\n"}) == (
        "pyproject.toml",
        None,
    )
    assert tools_pure.ruff_config_name({"pyproject.toml": "[tool.black]\n"}) == (None, None)
    assert tools_pure.ruff_config_name({".ruff.toml": "", "ruff.toml": ""}) == (".ruff.toml", None)
    name, problem = tools_pure.ruff_config_name({"pyproject.toml": "[tool"})
    assert name is None and "pyproject.toml" in problem


def test_config_rule_is_one_rule_set_with_the_settings_file_as_source() -> None:
    rule = config_rule(RuffConfig("pyproject.toml", "[tool.ruff]\n", "b" * 40))
    assert (rule.id, rule.origin, rule.fix, rule.check.tool) == (
        "ruff", "tool_config", "mechanical", "ruff"
    )  # fmt: skip
    assert (rule.source.file, rule.source.section) == ("pyproject.toml", "[tool.ruff]")


def test_valid_codes() -> None:
    assert valid_codes(["f401", "F401", "PLR2004"]) == ("F401", "PLR2004")
    assert valid_codes([]) is None and valid_codes(["F"]) is None and valid_codes(["-x"]) is None
    assert valid_codes([f"F{n}" for n in range(11)]) is None


def test_setup_label_names_who_chose_the_version() -> None:
    assert RuffSetup("0.17.5").label == "ruff 0.17.5 (기본 버전)"
    assert RuffSetup("0.17.4", "requirements.txt").label == "ruff 0.17.4 (requirements.txt가 고정)"
