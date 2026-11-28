"""app/worker/sandbox/egress_pure.py 단위 테스트: 주소 판정, 요청 읽기, 프록시 기록 줄, 기한."""

from ipaddress import ip_address

import pytest

from app.worker.sandbox.egress_pure import (
    BadRequest,
    Denied,
    Event,
    Request,
    expired,
    is_public,
    parse_event,
    parse_request,
    public_addresses,
)

PUBLIC = [
    "8.8.8.8",
    "151.101.2.132",
    "20.200.245.247",
    "2606:4700::1111",
    "::ffff:8.8.8.8",  # IPv4-mapped 공용
    "64:ff9b::808:808",  # NAT64 공용
]
NOT_PUBLIC = {
    "10.1.2.3": "사설",
    "172.16.0.1": "사설",
    "172.17.0.1": "Docker bridge 게이트웨이",
    "192.168.65.254": "Docker Desktop의 host.docker.internal",
    "127.0.0.1": "루프백",
    "127.255.255.254": "루프백",
    "169.254.169.254": "링크 로컬(클라우드 메타데이터)",
    "100.64.0.1": "CGNAT",
    "100.127.255.254": "CGNAT",
    "0.0.0.0": "지정 안 됨",
    "255.255.255.255": "브로드캐스트",
    "224.0.0.1": "멀티캐스트",
    "239.255.255.250": "멀티캐스트",
    "240.0.0.1": "예약",
    "192.0.2.1": "문서용",
    "198.18.0.1": "벤치마크용",
    "::1": "IPv6 루프백",
    "::": "IPv6 지정 안 됨",
    "fe80::1": "IPv6 링크 로컬",
    "fe80::1%eth0": "영역이 붙은 IPv6 링크 로컬",
    "fc00::1": "IPv6 고유 로컬",
    "fd12:3456::1": "IPv6 고유 로컬",
    "fec0::1": "IPv6 사이트 로컬",
    "ff02::1": "IPv6 멀티캐스트",
    "ff0e::1": "IPv6 전역 범위 멀티캐스트",
    "::ffff:127.0.0.1": "IPv4-mapped 루프백",
    "::ffff:10.0.0.1": "IPv4-mapped 사설",
    "::ffff:169.254.169.254": "IPv4-mapped 링크 로컬",
    "64:ff9b::a00:1": "NAT64 사설",
    "64:ff9b::7f00:1": "NAT64 루프백",
    "2002:c0a8:101::1": "6to4 사설",
    "2002:808:808::1": "6to4 공용도 거절",
    "2001::1": "Teredo",
    "2001:db8::1": "IPv6 문서용",
}


@pytest.mark.parametrize("text", PUBLIC)
def test_global_unicast_addresses_are_public(text: str) -> None:
    assert is_public(ip_address(text))
    assert public_addresses([text]) == (text,)


@pytest.mark.parametrize("text", NOT_PUBLIC)
def test_every_other_address_class_is_denied(text: str) -> None:
    assert not is_public(ip_address(text)), NOT_PUBLIC[text]
    assert isinstance(public_addresses([text]), Denied)


def test_one_private_address_among_public_ones_denies_the_name() -> None:
    got = public_addresses(["8.8.8.8", "2606:4700::1111", "10.0.0.5"])
    assert got == Denied("10.0.0.5: 공용 주소가 아님")
    assert isinstance(public_addresses(["::1", "8.8.8.8"]), Denied)


def test_public_addresses_keep_resolver_order_without_duplicates() -> None:
    got = public_addresses(["2606:4700::1111", "8.8.8.8", "2606:4700::1111", "8.8.4.4"])
    assert got == ("2606:4700::1111", "8.8.8.8", "8.8.4.4")


def test_no_address_or_unreadable_address_is_denied() -> None:
    assert public_addresses([]) == Denied("풀린 주소가 없음")
    assert isinstance(public_addresses(["not-an-ip"]), Denied)


def test_connect_request_names_host_and_port() -> None:
    req = parse_request(b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n")
    assert req == Request("CONNECT", "github.com", 443, b"", 0)
    v6 = parse_request(b"CONNECT [2606:4700::1111]:443 HTTP/1.1\r\n\r\n")
    assert isinstance(v6, Request) and (v6.host, v6.port) == ("2606:4700::1111", 443)


@pytest.mark.parametrize(
    "target",
    ["github.com", "github.com:0", "github.com:65536", "github.com:x", ":443", "u@h.com:443",
     "[::1]x:443", "github.com:4\u00b2"],
)  # fmt: skip
def test_connect_without_a_valid_port_or_with_userinfo_is_bad(target: str) -> None:
    head = f"CONNECT {target} HTTP/1.1\r\n\r\n".encode("latin-1")
    assert isinstance(parse_request(head), BadRequest)


def test_plain_http_is_rewritten_to_one_request_for_the_origin() -> None:
    head = (
        b"GET http://deb.debian.org/debian/dists/bookworm/InRelease?x=1 HTTP/1.1\r\n"
        b"Host: deb.debian.org\r\nUser-Agent: apt\r\nConnection: keep-alive, X-Hop\r\n"
        b"X-Hop: 1\r\nProxy-Connection: keep-alive\r\nProxy-Authorization: Basic eA==\r\n"
        b"Keep-Alive: 300\r\n\r\n"
    )
    req = parse_request(head)
    assert isinstance(req, Request)
    assert (req.method, req.host, req.port, req.body_length) == ("GET", "deb.debian.org", 80, 0)
    assert req.forward == (
        b"GET /debian/dists/bookworm/InRelease?x=1 HTTP/1.1\r\n"
        b"Host: deb.debian.org\r\nUser-Agent: apt\r\nConnection: close\r\n\r\n"
    )


def test_plain_http_without_path_or_host_header_gets_both() -> None:
    req = parse_request(b"GET http://example.com:8080 HTTP/1.0\r\n\r\n")
    assert isinstance(req, Request) and (req.host, req.port) == ("example.com", 8080)
    assert req.forward == b"GET / HTTP/1.0\r\nHost: example.com:8080\r\nConnection: close\r\n\r\n"


def test_plain_http_body_length_is_read_from_content_length() -> None:
    req = parse_request(b"POST http://h.com/p HTTP/1.1\r\nContent-Length: 12\r\n\r\n")
    assert isinstance(req, Request) and req.body_length == 12


@pytest.mark.parametrize(
    "head",
    [
        b"GET / HTTP/1.1\r\nHost: h.com\r\n\r\n",  # 프록시 요청이 아님
        b"GET https://h.com/ HTTP/1.1\r\n\r\n",  # https는 CONNECT로만
        b"GET ftp://h.com/ HTTP/1.1\r\n\r\n",
        b"GET http://user@h.com/ HTTP/1.1\r\n\r\n",
        b"POST http://h.com/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
        b"POST http://h.com/ HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n",
        b"POST http://h.com/ HTTP/1.1\r\nContent-Length: -1\r\n\r\n",
        b"POST http://h.com/ HTTP/1.1\r\nContent-Length: 1\xb2\r\n\r\n",
        b"GET http://h.com/ HTTP/1.1\r\nno colon\r\n\r\n",
        b"GET http://h.com/ HTTP/1.1\r\n Folded: x\r\n\r\n",
        b"GARBAGE\r\n\r\n",
    ],
)
def test_requests_the_proxy_does_not_take_are_bad(head: bytes) -> None:
    assert isinstance(parse_request(head), BadRequest)


def test_event_lines_round_trip_and_reject_other_shapes() -> None:
    event = Event("denied", "CONNECT", "host.docker.internal", 6379, "192.168.65.254: 공용 아님")
    assert parse_event(event.line()) == event
    assert Event("allowed", "CONNECT", "::1", 443).target() == "[::1]:443"
    good = '{"verdict": "allowed", "method": "GET", "host": "h", "port": 80, "detail": ""}'
    assert parse_event(good) == Event("allowed", "GET", "h", 80)
    for line in ("", "not json", "[]", good.replace("allowed", "maybe"),
                 good.replace("80", '"80"'), good.replace("80", "true"),
                 good.replace('"detail": ""', '"detail": 1')):  # fmt: skip
        assert parse_event(line) is None


def test_only_names_past_their_deadline_expire() -> None:
    listing = "old\t100\nlive\t300\nexact\t200\nbroken\tsoon\n\t50\nnolabel\t\n"
    assert expired(listing, now=200) == ("old",)
