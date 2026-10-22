import ipaddress

import pytest

from agentbuilder_sandbox.net.filter import (
    AddressGuard,
    NetworkRequest,
    canonicalize_host,
    decide,
    parse_network,
    parse_rule,
)
from agentbuilder_sandbox.policy import NetworkPolicy


@pytest.mark.parametrize(
    ("host", "canon"),
    [
        ("Example.COM.", "example.com"),
        ("127.1", "127.0.0.1"),
        ("2130706433", "127.0.0.1"),
        ("0x7f.0.0.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("[::FFFF:127.0.0.1]", "127.0.0.1"),
        ("[2001:DB8:0:0::1]", "2001:db8::1"),
        ("under_score.example.com", "under_score.example.com"),
    ],
)
def test_canonicalize(host: str, canon: str) -> None:
    assert canonicalize_host(host) == canon


@pytest.mark.parametrize(
    "host",
    [
        "",
        "evil.com\x00.ok.com",
        "a b.com",
        "evil.com\r\nX: y",
        "fe80::1%eth0",
        "bücher.de",
        "1.2.3.999",
        "a..b.com",
        "-a.com",
        "x" * 300,
    ],
)
def test_canonicalize_rejects(host: str) -> None:
    assert canonicalize_host(host) is None


@pytest.mark.parametrize(
    "entry",
    [
        "*.com",
        "evil.com/path",
        "http://x.com",
        "2001:db8::1:443",
        "*",
        "a*.com",
        "127.1",
        "x.com:0",
        "x.com:70000",
        "nodot",
    ],
)
def test_bad_allow_entries(entry: str) -> None:
    with pytest.raises(ValueError):
        parse_rule(entry, allow_star=False)


def test_policy_validates_domains() -> None:
    with pytest.raises(ValueError):
        NetworkPolicy(allowed_domains=["*.com"])
    assert NetworkPolicy(denied_domains=["*", "*:22"]).denied_rules()[1].port == 22


def test_rule_matching() -> None:
    wild, exact, port, v6 = parse_network(
        ["*.example.com", "pypi.org", "api.x.com:443", "[::1]:8080"], allow_star=False
    )
    assert wild.matches("a.example.com", 80) and wild.matches("a.b.example.com", 1)
    assert not wild.matches("example.com", 80)  # strict subdomains only
    assert not wild.matches("evilexample.com", 80)
    assert exact.matches("pypi.org", 443) and not exact.matches("x.pypi.org", 443)
    assert port.matches("api.x.com", 443) and not port.matches("api.x.com", 80)
    assert v6.matches("::1", 8080)


async def test_decision_order() -> None:
    allowed = parse_network(["*.example.com"], allow_star=False)
    denied = parse_network(["bad.example.com", "*:22"], allow_star=True)
    extra = parse_network(["pypi.org"], allow_star=False)

    async def d(host: str, port: int = 443, **kw):
        return await decide(host, port, exec_id="e", allowed=allowed, denied=denied, **kw)

    assert (await d("ok.example.com")).allowed
    assert not (await d("bad.example.com")).allowed
    assert not (await d("ok.example.com", 22)).allowed
    assert not (await d("pypi.org")).allowed
    assert (await d("pypi.org", extra_allowed=extra)).allowed
    assert (await d("OK.Example.Com.")).host == "ok.example.com"
    assert (await d("evil\x00.example.com")).reason == "malformed host"


async def test_approval_needs_literal_true() -> None:
    asked: list[NetworkRequest] = []

    async def truthy(req: NetworkRequest) -> bool:
        asked.append(req)
        return "yes"  # type: ignore[return-value]

    async def yes(req: NetworkRequest) -> bool:
        return True

    async def boom(req: NetworkRequest) -> bool:
        raise RuntimeError

    kw = {"exec_id": "e1", "allowed": [], "denied": parse_network(["no.com"], allow_star=True)}
    assert not (await decide("x.com", 443, approve=truthy, **kw)).allowed
    assert asked == [NetworkRequest("x.com", 443, "e1")]
    assert (await decide("x.com", 443, approve=yes, **kw)).allowed
    assert not (await decide("no.com", 443, approve=yes, **kw)).allowed  # deny list first
    assert (await decide("x.com", 443, approve=boom, **kw)).reason == "approval failed"


@pytest.mark.parametrize(
    ("addr", "reason"),
    [
        ("127.0.0.1", "a loopback address"),
        ("::1", "a loopback address"),
        ("::ffff:127.0.0.1", "a loopback address"),
        ("64:ff9b::7f00:1", "a loopback address"),
        ("2002:7f00:1::", "a loopback address"),
        ("169.254.169.254", "a link-local address"),
        ("fd00:ec2::254", "a cloud metadata address"),
        ("100.100.100.200", "a cloud metadata address"),
        ("10.1.2.3", "a private address"),
        ("::ffff:192.168.1.1", "a private address"),
        ("0.0.0.0", "an unspecified address"),
        ("203.0.113.9", "one of this host's addresses"),
        ("93.184.215.14", None),
        ("2606:4700::1", None),
    ],
)
def test_address_guard(addr: str, reason: str | None) -> None:
    guard = AddressGuard(local=lambda: {ipaddress.ip_address("203.0.113.9")})
    assert guard.denial("example.com", 443, ipaddress.ip_address(addr)) == reason


def test_address_guard_options() -> None:
    ip = ipaddress.ip_address
    lenient = AddressGuard(deny_private=False, extra=["198.51.100.0/24"], local=set)
    assert lenient.denial("intranet.corp", 80, ip("10.0.0.5")) is None
    assert lenient.denial("x.com", 80, ip("198.51.100.7")) == "a listed address"
    literal = AddressGuard(allowed_literals=parse_network(["127.0.0.1:3000"], allow_star=False))
    assert literal.denial("myapp.test", 3000, ip("127.0.0.1")) is None
    assert literal.denial("myapp.test", 3001, ip("127.0.0.1")) == "a loopback address"
    assert literal.denial("localhost", 80, ip("127.0.0.1")) is None
    assert literal.denial("localhost", 80, ip("93.184.215.14")) == "a non-loopback address"


async def test_resolve_literal_and_localhost() -> None:
    guard = AddressGuard(local=set)
    assert await guard.resolve("93.184.215.14", 80) == "93.184.215.14"
    assert ipaddress.ip_address(await guard.resolve("localhost", 80)).is_loopback
