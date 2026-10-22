import os
import sys
from pathlib import Path

import pytest

from agentbuilder_sandbox.linux.bwrap import MountPlan, plan_mounts
from agentbuilder_sandbox.policy import FilesystemPolicy

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")


def triples(args: list[str]) -> list[tuple[str, ...]]:
    """Group the flat mount args into (flag, operands...) tuples."""
    out: list[tuple[str, ...]] = []
    i = 0
    while i < len(args):
        n = 1 if args[i] == "--tmpfs" or args[i] in ("--dev", "--proc") else 2
        out.append(tuple(args[i : i + n + 1]))
        i += n + 1
    return out


def plan(
    tmp_path: Path,
    *,
    deny_read: list[str] = [],  # noqa: B006
    allow_read: list[str] = [],  # noqa: B006
    allow_write: list[str] = [],  # noqa: B006
    deny_write: list[str] = [],  # noqa: B006
) -> MountPlan:
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    fs = FilesystemPolicy(
        deny_read=deny_read, allow_read=allow_read, allow_write=allow_write, deny_write=deny_write
    )
    return plan_mounts(fs, empty_dir=str(empty))


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / ".git" / "objects").mkdir(parents=True)
    (ws / "src").mkdir()
    return ws


def test_order_and_basics(tmp_path: Path, ws: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    p = plan(tmp_path, allow_write=[str(ws)], deny_read=[str(secret)])
    m = triples(p.args)
    assert m[0] == ("--ro-bind", "/", "/")
    assert ("--bind", str(ws), str(ws)) in m
    assert ("--tmpfs", str(secret)) in m
    assert m[-1][0] == "--ro-bind" and m[-1][1].endswith("empty")
    assert p.write_roots == [str(ws)]


def test_dangerous_files_and_pins(tmp_path: Path, ws: Path) -> None:
    (ws / ".bashrc").write_text("")
    (ws / "sub" / ".git").mkdir(parents=True)
    (ws / "sub" / ".vscode").mkdir()
    p = plan(tmp_path, allow_write=[str(ws)])
    m = triples(p.args)
    assert ("--ro-bind", str(ws / ".bashrc"), str(ws / ".bashrc")) in m
    assert ("--ro-bind", "/dev/null", str(ws / ".git" / "hooks")) in m  # absent: placeholder
    assert ("--ro-bind", "/dev/null", str(ws / "sub" / ".git" / "hooks")) in m
    assert ("--ro-bind", str(ws / "sub" / ".vscode"), str(ws / "sub" / ".vscode")) in m
    # absent `.claude/commands`: an empty directory at the first missing component
    assert ("--ro-bind", str(tmp_path / "empty"), str(ws / ".claude")) in m
    assert str(ws / ".git" / "hooks") in p.placeholders
    # pins: below the write root, bound before the root's own bind
    pin = ("--ro-bind", str(ws / ".git"), str(ws / ".git"))
    assert m.index(pin) < m.index(("--bind", str(ws), str(ws)))


def test_scratch_write_has_no_dangerous_protection(tmp_path: Path, ws: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    p = plan_mounts(
        FilesystemPolicy(allow_write=[str(ws)], scratch_write=[str(home)]),
        empty_dir=str(tmp_path),
    )
    assert ("--bind", str(home), str(home)) in triples(p.args)
    assert not any(x.startswith(str(home) + "/") for x in p.args)
    assert str(ws / ".bashrc") in p.placeholders


def test_write_deny_wins_from_above(tmp_path: Path, ws: Path) -> None:
    p = plan(tmp_path, allow_write=[str(ws), str(ws / "src")], deny_write=[str(ws)])
    m = triples(p.args)
    assert p.write_roots == []
    assert not any(t[0] == "--bind" and t[1].startswith(str(ws)) for t in m)


def test_read_rules_specific_wins(tmp_path: Path, ws: Path) -> None:
    home = tmp_path / "home"
    (home / "proj" / "keys").mkdir(parents=True)
    (home / "notes.txt").write_text("x")
    p = plan(
        tmp_path,
        deny_read=[str(home), str(home / "proj" / "keys"), str(ws / "src")],
        allow_read=[str(home / "proj"), str(ws)],  # ws is not hidden: no-op
        allow_write=[str(ws)],
    )
    m = triples(p.args)
    order = [
        m.index(t)
        for t in [
            ("--tmpfs", str(home)),
            ("--ro-bind", str(home / "proj"), str(home / "proj")),
            ("--tmpfs", str(home / "proj" / "keys")),
        ]
    ]
    assert order == sorted(order)
    assert ("--ro-bind", str(ws), str(ws)) not in m
    assert m.index(("--bind", str(ws), str(ws))) < m.index(("--tmpfs", str(ws / "src")))


def test_write_deny_inside_hidden_region_is_not_rebound(tmp_path: Path, ws: Path) -> None:
    (ws / "private").mkdir()
    (ws / "private" / ".bashrc").write_text("")
    p = plan(tmp_path, allow_write=[str(ws)], deny_read=[str(ws / "private")])
    bashrc = str(ws / "private" / ".bashrc")
    assert not any(bashrc in t for t in triples(p.args))


def test_symlinked_paths_are_resolved(tmp_path: Path, ws: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(ws)
    p = plan(tmp_path, allow_write=[str(link)], deny_write=[str(link / "src")])
    m = triples(p.args)
    assert ("--bind", str(ws), str(ws)) in m
    assert ("--ro-bind", str(ws / "src"), str(ws / "src")) in m


def test_rejects_root_write_and_globs(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        plan(tmp_path, allow_write=["/"])
    with pytest.raises(ValueError):
        FilesystemPolicy(deny_read=["/home/*/.ssh"])
    with pytest.raises(ValueError):
        FilesystemPolicy(allow_write=["relative/path"])


def test_missing_paths_are_skipped(tmp_path: Path) -> None:
    p = plan(tmp_path, allow_write=[str(tmp_path / "nope")], deny_read=[str(tmp_path / "gone")])
    assert p.write_roots == []
    assert not any("nope" in a or "gone" in a for a in p.args)
    assert os.path.exists(tmp_path / "empty")
