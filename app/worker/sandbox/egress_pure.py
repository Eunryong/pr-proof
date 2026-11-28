"""굽는 컨테이너의 바깥 연결 판정 (DESIGN 8.1, 위협 T41).

프록시(`egress_proxy.py`)가 컨테이너 안에서 이 모듈로 요청을 읽고 주소를 판정해 한 줄씩 남긴다.
호스트는 그 줄을 `parse_event`로 읽는다. 받는 곳의 목록은 없다. 풀린 주소가 모두 공용 주소일 때만
연결한다.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from ipaddress import IPv4Address, IPv6Address, IPv6Network, ip_address
from typing import Final, Literal, get_args

PORT: Final = 3128
LOG_PATH: Final = "/tmp/egress.jsonl"
MAX_HEAD_BYTES: Final = 64 * 1024
# 네트워크와 프록시 컨테이너에 붙이는 라벨. 값은 그 뒤로는 아무도 쓰지 않는 시각(유닉스 초)이다
UNTIL_LABEL: Final = "pr-proof.egress.until"

# 안의 IPv4 주소로 가는 IPv6 주소. 안의 주소로 판정한다. IPv4-mapped는 `ipv4_mapped`가 꺼낸다
_NAT64: Final = IPv6Network("64:ff9b::/96")
# 요청 머리에서 프록시가 지우는 것. 연결 하나에만 해당하는 머리다(RFC 9110 7.6.1)
_HOP_BY_HOP: Final = frozenset(
    {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "te", "trailer",
     "upgrade"}
)  # fmt: skip

# allowed: 연결함. denied: 공용이 아닌 주소가 섞임. unresolved: 이름이 없음(NXDOMAIN 등).
# dns_failed: 이름 서버가 답하지 못함. refused: 공용 주소가 연결을 거절함. unreachable: 공용 주소에
# 닿지 못함(시간 초과, 경로 없음). bad_request: 프록시가 받지 않는 요청
Verdict = Literal[
    "allowed", "denied", "unresolved", "dns_failed", "refused", "unreachable", "bad_request"
]
_VERDICTS: Final = frozenset(get_args(Verdict))


@dataclass(frozen=True)
class Event:
    """프록시가 받은 요청 하나와 판정."""

    verdict: Verdict
    method: str
    host: str
    port: int
    detail: str = ""

    def line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    def target(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def parse_event(line: str) -> Event | None:
    """프록시가 남긴 한 줄. 모양이 다르면 None이다(프록시는 굽는 코드와 같은 네트워크에 있다)."""
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in _VERDICTS:
        return None
    method, host, port, detail = (data.get(k) for k in ("method", "host", "port", "detail"))
    if not (isinstance(method, str) and isinstance(host, str) and isinstance(detail, str)):
        return None
    if not isinstance(port, int) or isinstance(port, bool):
        return None
    return Event(data["verdict"], method, host, port, detail)


@dataclass(frozen=True)
class Denied:
    reason: str


def is_public(address: IPv4Address | IPv6Address) -> bool:
    """공용 유니캐스트 주소인지.

    `is_global`(IANA 특수 목적 주소 등록부에서 전역이 아닌 것을 뺀다: 사설, 루프백, 링크 로컬,
    CGNAT 100.64/10, 지정 안 됨, 문서용, 벤치마크용, 예약)에 더해 `is_global`이 참으로 두는
    멀티캐스트와 IPv6 사이트 로컬(fec0::/10)을 뺀다. IPv4-mapped와 NAT64(64:ff9b::/96)는 안의 IPv4
    주소로 판정한다. 6to4(2002::/16)와 Teredo(2001::/32)는 안의 주소와 무관하게 거절한다. 둘 다
    IPv4 너머로 터널을 거치는 옛 전환 방식이고 Python 판에 따라 `is_global`의 답이 다르다.
    """
    if isinstance(address, IPv6Address):
        if address.ipv4_mapped is not None:
            return is_public(address.ipv4_mapped)
        if address in _NAT64:
            return is_public(IPv4Address(int(address) & 0xFFFFFFFF))
        if address.sixtofour is not None or address.teredo is not None or address.is_site_local:
            return False
    return address.is_global and not address.is_multicast


def public_addresses(resolved: Sequence[str]) -> tuple[str, ...] | Denied:
    """이름을 푼 주소 전부가 공용이면 그 주소들(중복 없이, 받은 순서), 하나라도 아니면 Denied.

    프록시는 여기서 돌려준 주소에만 연결한다. 이름을 다시 풀지 않으므로 판정과 연결 사이에 DNS
    답이 바뀌어도(DNS rebinding) 다른 주소로 가지 않는다.
    """
    if not resolved:
        return Denied("풀린 주소가 없음")
    checked: list[str] = []
    for text in resolved:
        try:
            address = ip_address(text)
        except ValueError:
            return Denied(f"주소로 읽지 못함: {text!r}")
        if not is_public(address):
            return Denied(f"{address}: 공용 주소가 아님")
        if text not in checked:
            checked.append(text)
    return tuple(checked)


@dataclass(frozen=True)
class Request:
    """프록시가 받은 요청. forward는 평문 HTTP에서 원 서버로 보낼 머리이고 CONNECT에서는 비었다."""

    method: str
    host: str
    port: int
    forward: bytes
    body_length: int


@dataclass(frozen=True)
class BadRequest:
    reason: str


def parse_request(head: bytes) -> Request | BadRequest:
    """`CONNECT host:port`와 절대 URI(`http://…`)의 평문 HTTP 요청을 읽는다. head는 빈 줄까지다.

    평문 HTTP의 머리는 원 서버에 보낼 모양(경로만 남은 요청 줄, 연결 하나에만 해당하는 머리를 뺌,
    `Connection: close`)으로 바꾼다. 요청 하나에 연결 하나다. 같은 연결로 다른 호스트의 요청을
    이어 보내 확인한 주소로 흘러가지 않게 한다.
    """
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return BadRequest(f"요청 줄을 읽지 못함: {lines[0][:200]!r}")
    method, target, version = parts
    if method == "CONNECT":
        authority = _authority(target)
        if isinstance(authority, BadRequest):
            return authority
        return Request(method, authority[0], authority[1], b"", 0)
    if target[:7].lower() != "http://":
        return BadRequest(f"CONNECT나 http:// 절대 URI만 받음: {target[:200]!r}")
    rest = target[7:]
    cut = min((i for i in (rest.find("/"), rest.find("?")) if i >= 0), default=len(rest))
    authority = _authority(rest[:cut], default_port=80)
    if isinstance(authority, BadRequest):
        return authority
    path = rest[cut:] if rest[cut:].startswith("/") else "/" + rest[cut:]
    headers = _forward_headers([line for line in lines[1:] if line], host=rest[:cut])
    if isinstance(headers, BadRequest):
        return headers
    kept, body_length = headers
    forward = "\r\n".join([f"{method} {path} {version}", *kept, "", ""]).encode("latin-1")
    return Request(method, authority[0], authority[1], forward, body_length)


def _authority(text: str, default_port: int | None = None) -> tuple[str, int] | BadRequest:
    if "@" in text or not text:
        return BadRequest(f"호스트를 읽지 못함: {text[:200]!r}")
    if text.startswith("["):
        host, _, after = text[1:].partition("]")
        port_text = after.removeprefix(":") if after.startswith(":") else None
        if after and port_text is None:
            return BadRequest(f"호스트를 읽지 못함: {text[:200]!r}")
    else:
        host, sep, port_text = text.rpartition(":")
        if not sep:
            host, port_text = text, None
    if port_text is None:
        if default_port is None:
            return BadRequest(f"포트가 없음: {text[:200]!r}")
        return host, default_port
    if not _digits(port_text) or not 0 < int(port_text) < 65536 or not host:
        return BadRequest(f"호스트나 포트를 읽지 못함: {text[:200]!r}")
    return host, int(port_text)


def _forward_headers(lines: list[str], host: str) -> tuple[list[str], int] | BadRequest:
    named: list[tuple[str, str]] = []
    for line in lines:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            return BadRequest(f"머리를 읽지 못함: {line[:200]!r}")
        named.append((name.lower(), value.strip()))
    listed = {
        token.strip().lower()
        for name, value in named
        if name in ("connection", "proxy-connection")
        for token in value.split(",")
    }
    if any(name == "transfer-encoding" for name, _ in named):
        return BadRequest("Transfer-Encoding이 있는 요청 본문은 받지 않음")
    lengths = {value for name, value in named if name == "content-length"}
    if len(lengths) > 1 or any(not _digits(v) for v in lengths):
        return BadRequest(f"Content-Length를 읽지 못함: {sorted(lengths)}")
    kept = [
        line
        for line, (name, _) in zip(lines, named, strict=True)
        if name not in _HOP_BY_HOP and name not in listed
    ]
    if not any(name == "host" for name, _ in named):
        kept.append(f"Host: {host}")
    return [*kept, "Connection: close"], int(next(iter(lengths), "0"))


def expired(listing: str, now: float) -> tuple[str, ...]:
    """`이름<탭>기한` 줄들에서 기한이 지난 이름. 기한을 읽지 못하는 줄은 건드리지 않는다."""
    names: list[str] = []
    for line in listing.splitlines():
        name, _, until = line.partition("\t")
        if name and _digits(until) and int(until) < now:
            names.append(name)
    return tuple(names)


def _digits(text: str) -> bool:
    """ASCII 숫자만. `str.isdigit`은 latin-1의 위 첨자(²)도 참이라 `int`가 실패한다."""
    return text.isascii() and text.isdigit()
