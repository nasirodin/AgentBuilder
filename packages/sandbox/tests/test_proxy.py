import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agentbuilder_sandbox.net.filter import AddressGuard, NetworkRequest, parse_network
from agentbuilder_sandbox.net.proxy import ConnectionRecord, FilteringProxy

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Unix sockets")


class Upstream:
    """Loopback server: answers HTTP requests with what it received; echoes anything else."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        first = await r.read(4096)
        if first.startswith(b"GET") or first.startswith(b"POST"):
            self.requests.append(first)
            body = b"hello"
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n" + body)
        else:
            w.write(b"echo:" + first)
        await w.drain()
        w.close()


@pytest.fixture
async def upstream() -> AsyncIterator[tuple[Upstream, int]]:
    up = Upstream()
    server = await asyncio.start_server(up.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield up, port
    server.close()


async def make_proxy(
    tmp_path: Path, allowed: list[str], approve=None
) -> tuple[FilteringProxy, list[ConnectionRecord]]:
    records: list[ConnectionRecord] = []
    proxy = FilteringProxy(
        str(tmp_path / "p.sock"),
        exec_id="e1",
        allowed=parse_network(allowed, allow_star=False),
        denied=parse_network(["blocked.example.com"], allow_star=True),
        extra_allowed=[],
        guard=AddressGuard(local=set, allowed_literals=parse_network(allowed, allow_star=False)),
        approve=approve,
        on_record=records.append,
    )
    await proxy.start()
    return proxy, records


async def talk(tmp_path: Path, data: bytes, then: bytes = b"") -> bytes:
    r, w = await asyncio.open_unix_connection(str(tmp_path / "p.sock"))
    w.write(data)
    await w.drain()
    if then:
        await asyncio.sleep(0.05)
        w.write(then)
        await w.drain()
    out = await asyncio.wait_for(r.read(65536 * 4), 5)
    while chunk := await asyncio.wait_for(r.read(65536), 5):
        out += chunk
    w.close()
    return out


async def test_absolute_http_request(tmp_path: Path, upstream: tuple[Upstream, int]) -> None:
    up, port = upstream
    proxy, records = await make_proxy(tmp_path, [f"127.0.0.1:{port}"])
    resp = await talk(
        tmp_path,
        f"GET http://127.0.0.1:{port}/a?b=1#frag HTTP/1.1\r\nHost: evil.com\r\n"
        "Proxy-Authorization: Basic x\r\nConnection: keep-alive, X-Secret\r\nX-Secret: s\r\n"
        "Accept: */*\r\n\r\n".encode(),
    )
    await proxy.close()
    assert resp.startswith(b"HTTP/1.1 200 OK") and resp.endswith(b"hello")
    sent = up.requests[0].decode()
    assert sent.startswith("GET /a?b=1 HTTP/1.1\r\n")
    assert f"Host: 127.0.0.1:{port}" in sent and "evil.com" not in sent
    assert "Proxy-Authorization" not in sent and "X-Secret" not in sent
    assert "Accept: */*" in sent and "Connection: close" in sent
    assert records[-1].allowed and records[-1].protocol == "http"


async def test_connect_tunnel(tmp_path: Path, upstream: tuple[Upstream, int]) -> None:
    _, port = upstream
    proxy, _ = await make_proxy(tmp_path, [f"127.0.0.1:{port}"])
    resp = await talk(tmp_path, f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode(), b"ping")
    await proxy.close()
    assert resp == b"HTTP/1.1 200 Connection Established\r\n\r\necho:ping"


@pytest.mark.parametrize(
    ("request_bytes", "status"),
    [
        (b"CONNECT blocked.example.com:443 HTTP/1.1\r\n\r\n", b"403 host is on the deny list"),
        (b"CONNECT other.example.org:443 HTTP/1.1\r\n\r\n", b"403 host is not on the allow list"),
        (b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n", b"403"),
        (b"CONNECT nonsense HTTP/1.1\r\n\r\n", b"400"),
        (b"GET /origin-form HTTP/1.1\r\nHost: x\r\n\r\n", b"400"),
        (b"GET https://x.com/ HTTP/1.1\r\n\r\n", b"400"),
        (b"GET ftp://x.com/ HTTP/1.1\r\n\r\n", b"400"),
        (
            b"POST http://127.0.0.1:9/ HTTP/1.1\r\nContent-Length: 4\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n",
            b"400 ambiguous message framing",
        ),
        (b"GET http://x.com/ HTTP/1.1\r\nX: a\r\n folded\r\n\r\n", b"400"),
    ],
)
async def test_refusals(tmp_path: Path, request_bytes: bytes, status: bytes) -> None:
    proxy, _ = await make_proxy(tmp_path, ["127.0.0.1:9"])
    resp = await talk(tmp_path, request_bytes)
    await proxy.close()
    assert resp.startswith(b"HTTP/1.1 " + status), resp


async def test_hostname_resolving_to_loopback_is_refused(tmp_path: Path) -> None:
    proxy, records = await make_proxy(tmp_path, ["localhost.example.com"])

    async def fake(host: str, port: int) -> str:
        from agentbuilder_sandbox.net.filter import AddressDenied

        raise AddressDenied(host, "resolved to a loopback address")

    proxy._guard.resolve = fake  # type: ignore[method-assign]
    resp = await talk(tmp_path, b"CONNECT localhost.example.com:443 HTTP/1.1\r\n\r\n")
    await proxy.close()
    assert resp.startswith(b"HTTP/1.1 403 resolved to a loopback address")
    assert not records[-1].allowed


async def test_socks5(tmp_path: Path, upstream: tuple[Upstream, int]) -> None:
    _, port = upstream
    proxy, records = await make_proxy(tmp_path, [f"127.0.0.1:{port}"])
    ok = await talk(
        tmp_path, b"\x05\x01\x00", b"\x05\x01\x00\x01\x7f\x00\x00\x01" + port.to_bytes(2) + b"hi"
    )
    name = b"blocked.example.com"
    denied = await talk(
        tmp_path, b"\x05\x01\x00", b"\x05\x01\x00\x03" + bytes([len(name)]) + name + b"\x01\xbb"
    )
    bind = await talk(tmp_path, b"\x05\x01\x00", b"\x05\x02\x00\x01\x7f\x00\x00\x01\x00\x50")
    await proxy.close()
    assert ok == b"\x05\x00" + b"\x05\x00\x00\x01" + bytes(6) + b"echo:hi"
    assert denied == b"\x05\x00" + b"\x05\x02\x00\x01" + bytes(6)
    assert bind[2:4] == b"\x05\x07"
    assert [r.allowed for r in records] == [True, False]


async def test_approval_is_asked_for_unknown_hosts(tmp_path: Path) -> None:
    asked: list[NetworkRequest] = []

    async def approve(req: NetworkRequest) -> bool:
        asked.append(req)
        return False

    proxy, records = await make_proxy(tmp_path, [], approve=approve)
    resp = await talk(tmp_path, b"CONNECT Unknown.Example.org.:443 HTTP/1.1\r\n\r\n")
    await proxy.close()
    assert resp.startswith(b"HTTP/1.1 403 not approved")
    assert asked == [NetworkRequest("unknown.example.org", 443, "e1")]
    assert records[0].host == "unknown.example.org"
