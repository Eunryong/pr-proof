from api import signature


def test_valid_signature() -> None:
    body = b'{"a": 1}'
    assert signature.verify("secret", body, signature.sign("secret", body))


def test_invalid_signatures() -> None:
    body = b'{"a": 1}'
    good = signature.sign("secret", body)
    assert not signature.verify("other", body, good)  # 다른 시크릿
    assert not signature.verify("secret", b'{"a": 2}', good)  # 바뀐 본문
    assert not signature.verify("secret", body, None)
    assert not signature.verify("secret", body, good.removeprefix("sha256="))  # 접두어 없음
