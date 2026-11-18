import pytest

from app.models.cache_key import model_cache_key


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
