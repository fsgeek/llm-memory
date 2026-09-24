"""The Windows side of a WSL machine, read through /mnt/c.

Claude Code, Claude Desktop's Cowork, and Codex on Windows keep their
transcripts under the Windows user profile, where a Linux install cannot run
hooks. On WSL the sweep reads them directly instead: same record formats,
recorded with the Windows path and the Windows machine id, because that is the
machine they came from.
"""

import re
import subprocess
from pathlib import Path


def _run(*argv):
    # Windows executables resolve relative paths against a Windows cwd.
    return subprocess.run(argv, capture_output=True, text=True, cwd="/mnt/c", timeout=30).stdout.strip()


def windows_profile():
    """The Windows user profile as a WSL path, or None when not on WSL."""
    if not Path("/proc/sys/fs/binfmt_misc/WSLInterop").exists():
        return None
    try:
        profile = _run("cmd.exe", "/c", "echo %USERPROFILE%")
        path = Path(_run("wslpath", "-u", profile)) if profile else None
    except (OSError, subprocess.SubprocessError):
        return None
    return path if path and path.is_dir() else None


def windows_machine_id():
    """Windows' MachineGuid, un-hyphenated like /etc/machine-id."""
    out = _run("reg.exe", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography", "/v", "MachineGuid")
    m = re.search(r"MachineGuid\s+REG_SZ\s+([0-9a-fA-F-]+)", out)
    return m.group(1).replace("-", "").lower() if m else None


def windows_path(path):
    """/mnt/c/Users/x/f.jsonl -> C:\\Users\\x\\f.jsonl"""
    m = re.match(r"/mnt/([a-z])/(.*)", str(path))
    return f"{m.group(1).upper()}:\\" + m.group(2).replace("/", "\\") if m else str(path)


def sources(profile):
    """(claude_root, codex_root, label) for each Windows transcript tree.
    Cowork keeps one Claude Code projects tree per task; its directory names
    are session paths, so the location, not the name, gives the label."""
    yield profile / ".claude" / "projects", profile / ".codex" / "sessions", None
    cowork = profile / "AppData" / "Roaming" / "Claude" / "local-agent-mode-sessions"
    for projects in sorted(cowork.glob("**/.claude/projects")):
        yield projects, None, "cowork"
