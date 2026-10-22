"""Mandatory deny-write list: files inside writable areas that the *user's* host tools may act
on (shell rc files, git hooks/config, editor and agent configs). Union of srt's list
(`sandbox-utils.ts` DANGEROUS_FILES / directories, `linuxGetMandatoryDenyPaths`) and ours.

At each write root every entry is protected, existing or not (absent ones get a placeholder
for the duration of the exec). Below the root, entries are protected where they exist, down
to `SEARCH_DEPTH` directories; `.git` directories also get `hooks` and `config` protected even
when absent, so a hook cannot be installed and `git init` cannot be re-run there.
"""

import os
import stat
from pathlib import PurePosixPath

DANGEROUS_FILES = (
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".bash_logout",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".profile",
    ".gitconfig",
    ".gitmodules",
    ".ripgreprc",
    ".mcp.json",
    ".envrc",
    ".pre-commit-config.yaml",
    "sitecustomize.py",
    "usercustomize.py",
)
DANGEROUS_DIRS = (".vscode", ".idea", ".claude/commands", ".claude/agents")
GIT_PROTECTED = ("hooks", "config")
SEARCH_DEPTH = 3
_SKIP_DIRS = {"node_modules", ".venv", "__pycache__"}


def dangerous_paths(root: str, depth: int = SEARCH_DEPTH) -> list[str]:
    """Absolute paths under `root` that must be read-only inside the sandbox."""
    out: list[str] = [os.path.join(root, n) for n in (*DANGEROUS_FILES, *DANGEROUS_DIRS)]
    out += _git_paths(os.path.join(root, ".git"))
    files = set(DANGEROUS_FILES)
    dirs = {n for n in DANGEROUS_DIRS if "/" not in n}
    nested = {tuple(PurePosixPath(n).parts) for n in DANGEROUS_DIRS if "/" in n}

    def walk(path: str, level: int) -> None:
        try:
            entries = list(os.scandir(path))
        except OSError:
            if path != root and _owned_dir(path):
                out.append(path)  # unreadable for the scan: protect it whole
            return
        for e in entries:
            if level > 0:
                if e.name in files or e.name in dirs:
                    out.append(e.path)
                elif e.name == ".git":
                    out.extend(_git_paths(e.path))
            if e.is_dir(follow_symlinks=False) and e.name not in _SKIP_DIRS and e.name != ".git":
                if level > 0:
                    for parts in nested:
                        if parts[0] == e.name and os.path.lexists(os.path.join(e.path, *parts[1:])):
                            out.append(os.path.join(e.path, *parts[1:]))
                if level + 1 <= depth:
                    walk(e.path, level + 1)

    walk(root, 0)
    return list(dict.fromkeys(out))


def _git_paths(git: str) -> list[str]:
    try:
        st = os.lstat(git)
    except OSError:
        return []
    if stat.S_ISDIR(st.st_mode):
        return [os.path.join(git, n) for n in GIT_PROTECTED]
    return [git]  # worktree/submodule `.git` file, or a symlink: protect the entry itself


def _owned_dir(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()
