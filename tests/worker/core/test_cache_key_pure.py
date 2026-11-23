import pytest

from app.worker.core.cache_key_pure import model_cache_key, prompt_version


def _key(**parts: object) -> str:
    return model_cache_key("rules", "haiku", "1", parts)


def test_same_input_same_key() -> None:
    assert _key(a="x", b=["1", "2"]) == _key(b=["1", "2"], a="x")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ({"a": ["1", "2"]}, {"a": ["2", "1"]}),  # 목록 안의 순서는 키를 바꾼다
        ({"a": "ab", "b": "c"}, {"a": "a", "b": "bc"}),  # 값의 경계가 섞이지 않는다
        ({"a": "x"}, {"a": "y"}),
    ],
)
def test_different_input_different_key(left: dict, right: dict) -> None:
    assert _key(**left) != _key(**right)


def test_stage_model_and_prompt_version_are_part_of_the_key() -> None:
    base = model_cache_key("rules", "haiku", "1", {})
    assert base != model_cache_key("checker", "haiku", "1", {})
    assert base != model_cache_key("rules", "sonnet", "1", {})
    assert base != model_cache_key("rules", "haiku", "2", {})


@pytest.mark.parametrize("empty", ["stage", "model", "prompt_version"])
def test_empty_identity_is_rejected(empty: str) -> None:
    args = {"stage": "rules", "model": "haiku", "prompt_version": "1"}
    args[empty] = ""
    with pytest.raises(ValueError):
        model_cache_key(**args, parts={})


def test_prompt_version_changes_with_any_prompt_part() -> None:
    schema = {"type": "object"}
    version = prompt_version("지시", schema, "틀")
    assert version == prompt_version("지시", {"type": "object"}, "틀")
    assert version != prompt_version("지시.", schema, "틀")
    assert version != prompt_version("지시", {"type": "array"}, "틀")
    assert version != prompt_version("지시", schema, "틀 ")
