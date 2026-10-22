"""Host-side filtering proxy: HTTP (CONNECT + absolute-URI) and SOCKS5 on one Unix socket.

One proxy listens per exec, on a socket bind-mounted into that exec's sandbox only, so every
connection is attributed to its exec by construction (srt has to trust a username the client
presents). The first byte tells the protocols apart: 0x05 is SOCKS5, anything else HTTP.

Every connection: canonicalise the target, decide (see `filter.decide`), resolve once and
vet the addresses (`AddressGuard`), dial the vetted IP, then relay bytes. Nothing is
decrypted: decisions use the CONNECT/SOCKS target or the absolute URI's authority only.
"""

import asyncio
import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from agentbuilder_sandbox.net.filter import (
    AddressDenied,
    AddressGuard,
    ApprovalCallback,
    Decision,
    DomainRule,
    decide,
    sanitize,
)

HEAD_LIMIT = 64 * 1024
HEAD_TIMEOUT_S = 30.0
CONNECT_TIMEOUT_S = 30.0
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "upgrade",
}
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_AUTHORITY = re.compile(r"^(?:\[([^\]]+)\]|([^:\[\]]+)):(\d{1,5})$")


@dataclass(frozen=True)
class ConnectionRecord:
    exec_id: str
    protocol: str  # "http", "connect" or "socks5"
    host: str
    port: int
    allowed: bool
    reason: str


class _Refused(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class FilteringProxy:
    def __init__(
        self,
        socket_path: str,
        *,
        exec_id: str,
        allowed: list[DomainRule],
        denied: list[DomainRule],
        extra_allowed: list[DomainRule],
        guard: AddressGuard,
        approve: ApprovalCallback | None = None,
        on_record: Callable[[ConnectionRecord], None] = lambda r: None,
    ) -> None:
        self.socket_path = socket_path
        self.exec_id = exec_id
        self._allowed, self._denied, self._extra = allowed, denied, extra_allowed
        self._guard = guard
        self._approve = approve
        self._on_record = on_record
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path, limit=HEAD_LIMIT
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for t in list(self._tasks):
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

    # --- shared ---------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            first = await asyncio.wait_for(reader.readexactly(1), HEAD_TIMEOUT_S)
            if first == b"\x05":
                await self._socks5(reader, writer)
            elif first != b"\x04":  # SOCKS4 is not supported
                await self._http(first, reader, writer)
        except OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError:
            pass
        except ValueError:  # StreamReader limit exceeded
            pass
        finally:
            self._tasks.discard(task)
            writer.close()

    async def _authorize(self, protocol: str, host: str, port: int) -> tuple[str, str]:
        """Decide and resolve; returns (canonical host, address to dial) or raises _Refused."""
        d: Decision = await decide(
            host,
            port,
            exec_id=self.exec_id,
            allowed=self._allowed,
            denied=self._denied,
            extra_allowed=self._extra,
            approve=self._approve,
        )
        if not d.allowed:
            self._record(protocol, d.host, port, False, d.reason)
            raise _Refused(403, d.reason)
        try:
            address = await self._guard.resolve(d.host, port)
        except AddressDenied as e:
            self._record(protocol, d.host, port, False, e.reason)
            raise _Refused(403, e.reason) from None
        except OSError:
            self._record(protocol, d.host, port, False, "did not resolve")
            raise _Refused(502, "did not resolve") from None
        self._record(protocol, d.host, port, True, d.reason)
        return d.host, address

    def _record(self, protocol: str, host: str, port: int, allowed: bool, reason: str) -> None:
        self._on_record(
            ConnectionRecord(self.exec_id, protocol, sanitize(host, 255), port, allowed, reason)
        )

    async def _dial(self, address: str, port: int):
        return await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT_S)

    # --- HTTP -----------------------------------------------------------------------------

    async def _http(
        self, first: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        head = first + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
        try:
            method, target, headers = _parse_head(head)
            if method == "CONNECT":
                await self._connect(target, reader, writer)
            else:
                await self._forward(method, target, headers, reader, writer)
        except _Refused as e:
            writer.write(_refusal(e.status, e.reason))
            with contextlib.suppress(OSError):
                await writer.drain()

    async def _connect(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        m = _AUTHORITY.match(target)
        if not m or not 0 < int(m.group(3)) < 65536:
            raise _Refused(400, "bad CONNECT target")
        host, port = m.group(1) or m.group(2), int(m.group(3))
        _, address = await self._authorize("connect", host, port)
        try:
            up_reader, up_writer = await self._dial(address, port)
        except OSError, TimeoutError:
            raise _Refused(502, "upstream connection failed") from None
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await _relay(reader, writer, up_reader, up_writer)

    async def _forward(
        self,
        method: str,
        target: str,
        headers: list[tuple[str, str]],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        url = urlsplit(target)
        if url.scheme != "http" or not url.hostname:
            # origin-form, https:// (use CONNECT) and every other scheme
            raise _Refused(400, "only absolute http:// requests and CONNECT are supported")
        try:
            port = url.port or 80
        except ValueError:
            raise _Refused(400, "bad port") from None
        names = {n.lower() for n, _ in headers}
        cl = {v.strip() for n, v in headers if n.lower() == "content-length"}
        te = [v.strip().lower() for n, v in headers if n.lower() == "transfer-encoding"]
        if ("transfer-encoding" in names and "content-length" in names) or len(cl) > 1:
            raise _Refused(400, "ambiguous message framing")
        if te and te != ["chunked"]:
            raise _Refused(400, "unsupported transfer-encoding")
        host, address = await self._authorize("http", url.hostname, port)
        listed = {
            t.strip().lower() for n, v in headers if n.lower() == "connection" for t in v.split(",")
        }
        authority = f"[{host}]" if ":" in host else host
        if port != 80:
            authority += f":{port}"
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        out = [f"{method} {path} HTTP/1.1", f"Host: {authority}"]
        for n, v in headers:
            ln = n.lower()
            if ln in _HOP_BY_HOP or ln in listed or ln == "host":
                continue
            out.append(f"{n}: {v}")
        out.append("Connection: close")
        try:
            up_reader, up_writer = await self._dial(address, port)
        except OSError, TimeoutError:
            raise _Refused(502, "upstream connection failed") from None
        # One request per connection: the rest (body, if any) is relayed as-is, framed by the
        # Content-Length / chunked encoding the client declared and we validated.
        up_writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
        await _relay(reader, writer, up_reader, up_writer)

    # --- SOCKS5 ---------------------------------------------------------------------------

    async def _socks5(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        (n,) = await reader.readexactly(1)
        methods = await reader.readexactly(n)
        if 0 not in methods:
            writer.write(b"\x05\xff")
            return
        writer.write(b"\x05\x00")
        ver, cmd, _, atyp = await asyncio.wait_for(reader.readexactly(4), HEAD_TIMEOUT_S)
        if ver != 5:
            return
        if atyp == 1:
            host = ".".join(str(b) for b in await reader.readexactly(4))
        elif atyp == 3:
            (ln,) = await reader.readexactly(1)
            host = (await reader.readexactly(ln)).decode("ascii", "replace")
        elif atyp == 4:
            raw = await reader.readexactly(16)
            host = ":".join(raw[i : i + 2].hex() for i in range(0, 16, 2))
        else:
            writer.write(_socks_reply(8))
            return
        port = int.from_bytes(await reader.readexactly(2))
        if cmd != 1:  # CONNECT only; no BIND / UDP ASSOCIATE
            writer.write(_socks_reply(7))
            return
        try:
            _, address = await self._authorize("socks5", host, port)
        except _Refused as e:
            writer.write(_socks_reply(2 if e.status == 403 else 4))
            return
        try:
            up_reader, up_writer = await self._dial(address, port)
        except ConnectionRefusedError:
            writer.write(_socks_reply(5))
            return
        except OSError, TimeoutError:
            writer.write(_socks_reply(4))
            return
        writer.write(_socks_reply(0))
        await _relay(reader, writer, up_reader, up_writer)


def _parse_head(head: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not _TOKEN.match(parts[0]) or not parts[2].startswith("HTTP/1."):
        raise _Refused(400, "bad request line")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not _TOKEN.match(name):  # also refuses obsolete line folding
            raise _Refused(400, "bad header")
        if any(c in value for c in "\r\n\0"):
            raise _Refused(400, "bad header")
        headers.append((name, value.strip()))
    return parts[0], parts[1], headers


def _refusal(status: int, reason: str) -> bytes:
    phrase = sanitize(reason, 200) or "Forbidden"
    body = b"Connection blocked by network allowlist\n" if status == 403 else b""
    tag = b"X-Proxy-Error: blocked-by-sandbox\r\n" if status == 403 else b""
    return (
        f"HTTP/1.1 {status} {phrase}\r\nContent-Type: text/plain\r\n".encode("ascii")
        + tag
        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
        + body
    )


def _socks_reply(code: int) -> bytes:
    return bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0])


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except OSError:
        pass


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader,
    up_writer: asyncio.StreamWriter,
) -> None:
    try:
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
    finally:
        up_writer.close()
