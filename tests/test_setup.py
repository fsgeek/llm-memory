import io
import json
import os
import time
from importlib.metadata import version
from pathlib import Path

import pytest

from khipumaq import setup


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _session_end_handlers(settings):
    return [
        handler
        for group in settings.get("hooks", {}).get("SessionEnd", [])
        for handler in group.get("hooks", [])
    ]


def test_install_claude_migrates_legacy_entries_and_preserves_unrelated_data(
    tmp_path,
):
    settings_path = tmp_path / "home" / ".claude" / "settings.json"
    claude_json = tmp_path / "home" / ".claude.json"
    qhaway_hook = {"type": "command", "command": "/opt/qhaway session-end"}
    legacy_hook = {
        "type": "command",
        "command": (
            "PYTHONPATH=/old/checkout python -c "
            "'from llm_memory.ingest import main; main()'"
        ),
    }
    settings = {
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "SessionEnd": [
                {"matcher": "", "hooks": [qhaway_hook]},
                {"hooks": [legacy_hook]},
            ]
        },
    }
    config = {
        "numStartups": 17,
        "mcpServers": {
            "llm-memory": {
                "command": "python",
                "args": ["-m", "llm_memory.mcp_server"],
            },
            "unrelated": {"command": "unrelated-server", "args": []},
        },
        "projects": {
            "/project/legacy": {
                "mcpServers": {
                    "llm-memory": {
                        "command": "python",
                        "args": ["-m", "llm_memory.mcp_server"],
                    }
                }
            },
            "/project/same-name": {
                "mcpServers": {
                    "llm-memory": {
                        "command": "other-server",
                        "args": ["--database", "memories"],
                    }
                }
            },
        },
    }
    _write_json(settings_path, settings)
    _write_json(claude_json, config)

    setup.install_claude(settings_path, claude_json)

    installed_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    installed_config = json.loads(claude_json.read_text(encoding="utf-8"))
    handlers = _session_end_handlers(installed_settings)
    khipumaq_hooks = [
        handler
        for handler in handlers
        if "khipumaq ingest claude-session" in handler["command"]
    ]
    assert qhaway_hook in handlers
    assert all("llm_memory.ingest" not in handler["command"] for handler in handlers)
    assert khipumaq_hooks == [
        {
            "type": "command",
            "command": " ".join(
                setup.command_prefix() + ["ingest", "claude-session"]
            ),
            "timeout": 30,
        }
    ]
    assert installed_settings["permissions"] == {"allow": ["Read"]}
    assert "llm-memory" not in installed_config["mcpServers"]
    assert installed_config["mcpServers"]["unrelated"] == config["mcpServers"][
        "unrelated"
    ]
    prefix = setup.command_prefix()
    assert installed_config["mcpServers"]["khipumaq"] == {
        "type": "stdio",
        "command": prefix[0],
        "args": prefix[1:] + ["serve"],
        "env": {},
    }
    assert (
        "llm-memory"
        not in installed_config["projects"]["/project/legacy"]["mcpServers"]
    )
    assert installed_config["projects"]["/project/same-name"]["mcpServers"][
        "llm-memory"
    ] == config["projects"]["/project/same-name"]["mcpServers"]["llm-memory"]
    assert installed_config["numStartups"] == 17


def test_install_claude_twice_is_idempotent(tmp_path):
    settings_path = tmp_path / "settings.json"
    claude_json = tmp_path / ".claude.json"

    setup.install_claude(settings_path, claude_json)
    first_settings = settings_path.read_text(encoding="utf-8")
    first_config = claude_json.read_text(encoding="utf-8")
    setup.install_claude(settings_path, claude_json)

    assert settings_path.read_text(encoding="utf-8") == first_settings
    assert claude_json.read_text(encoding="utf-8") == first_config
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    handlers = _session_end_handlers(settings)
    assert sum(
        "khipumaq ingest claude-session" in handler["command"]
        for handler in handlers
    ) == 1
    assert list(json.loads(first_config)["mcpServers"]) == ["khipumaq"]


def test_uninstall_claude_removes_only_khipumaq_entries_and_empty_hook_groups(
    tmp_path,
):
    settings_path = tmp_path / "settings.json"
    claude_json = tmp_path / ".claude.json"
    qhaway_hook = {"type": "command", "command": "/opt/qhaway session-end"}
    khipumaq_hook = {
        "type": "command",
        "command": "/venv/bin/khipumaq ingest claude-session",
        "timeout": 30,
    }
    _write_json(
        settings_path,
        {
            "theme": "dark",
            "hooks": {
                "SessionEnd": [
                    {"matcher": "", "hooks": [qhaway_hook]},
                    {"hooks": [khipumaq_hook]},
                ]
            },
        },
    )
    _write_json(
        claude_json,
        {
            "preferences": {"verbose": True},
            "mcpServers": {
                "khipumaq": {"command": "/venv/bin/khipumaq", "args": ["serve"]},
                "other": {"command": "other-server", "args": []},
            },
        },
    )

    setup.uninstall_claude(settings_path, claude_json)

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["theme"] == "dark"
    assert _session_end_handlers(settings) == [qhaway_hook]
    assert all(group["hooks"] for group in settings["hooks"]["SessionEnd"])
    config = json.loads(claude_json.read_text(encoding="utf-8"))
    assert config == {
        "preferences": {"verbose": True},
        "mcpServers": {"other": {"command": "other-server", "args": []}},
    }


def test_hooks_list_reports_a_dead_codex_stderr_without_waiting(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    codex = tmp_path / "bin" / "codex"
    codex.parent.mkdir()
    codex.write_text("#!/bin/sh\nread -r request\necho boom >&2\nexit 1\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="boom"):
        setup._hooks_list(str(codex), codex_home)

    assert time.monotonic() - started < 3


def test_codex_binary_finds_nvm_install_when_path_has_no_codex(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    codex = fake_home / ".nvm" / "versions" / "node" / "v24.8.0" / "bin" / "codex"
    codex.parent.mkdir(parents=True)
    codex.write_text("#!/bin/sh\n", encoding="utf-8")
    codex.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(empty_path))
    monkeypatch.delenv("CODEX_BIN", raising=False)

    assert setup.codex_binary() == str(codex)
    assert os.access(codex, os.X_OK)


def test_codex_hook_returns_quickly_and_logs_detached_ingest(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    missing = tmp_path / "missing-rollout.jsonl"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    payload = json.dumps(
        {
            "hook_event_name": "SessionEnd",
            "session_id": "missing-session",
            "transcript_path": str(missing),
            "cwd": str(tmp_path / "project"),
        }
    )

    started = time.monotonic()
    result = setup.codex_hook(io.StringIO(payload))
    elapsed = time.monotonic() - started

    assert result == 0
    assert elapsed < 0.75
    log = codex_home / "log" / "khipumaq-hook.log"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log.exists() and "rollout not found" in log.read_text(encoding="utf-8"):
            break
        time.sleep(0.02)
    assert "rollout not found" in log.read_text(encoding="utf-8")
    assert str(missing) in log.read_text(encoding="utf-8")


def test_command_prefix_uses_this_checkouts_venv_binary():
    checkout = Path(__file__).resolve().parents[1]

    assert setup.command_prefix() == [str(checkout / ".venv" / "bin" / "khipumaq")]


def test_command_prefix_from_wheel_pins_the_installed_version(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    uvx = str(tmp_path / "bin" / "uvx")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(setup, "_checkout_root", lambda: None)
    monkeypatch.setattr(setup.shutil, "which", lambda command: uvx)

    assert setup.command_prefix() == [
        uvx,
        "--from",
        f"khipumaq=={version('khipumaq')}",
        "khipumaq",
    ]


def test_install_replaces_unpinned_uvx_hook_and_uninstall_removes_pinned_hook(
    tmp_path, monkeypatch
):
    fake_home = tmp_path / "home"
    codex_home = tmp_path / "codex-home"
    settings_path = fake_home / ".claude" / "settings.json"
    claude_json = fake_home / ".claude.json"
    uvx = str(tmp_path / "bin" / "uvx")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(setup, "_checkout_root", lambda: None)
    monkeypatch.setattr(setup.shutil, "which", lambda command: uvx)
    _write_json(
        settings_path,
        {
            "hooks": {
                "SessionEnd": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": (
                                    "uvx --python 3.14 khipumaq ingest "
                                    "claude-session"
                                ),
                                "timeout": 30,
                            }
                        ]
                    }
                ]
            }
        },
    )

    setup.install_claude(settings_path, claude_json)

    handlers = _session_end_handlers(
        json.loads(settings_path.read_text(encoding="utf-8"))
    )
    assert [handler["command"] for handler in handlers] == [
        " ".join(setup.command_prefix() + ["ingest", "claude-session"])
    ]
    assert "khipumaq ingest claude-session" in handlers[0]["command"]

    setup.uninstall_claude(settings_path, claude_json)

    uninstalled = json.loads(settings_path.read_text(encoding="utf-8"))
    assert _session_end_handlers(uninstalled) == []


def test_install_and_uninstall_windows_clients_preserve_other_config(tmp_path):
    profile = tmp_path / "windows-profile"
    claude_code, claude_desktop = setup.windows_client_configs(profile)
    code_config = {
        "theme": "dark",
        "mcpServers": {
            "other": {"command": "other-server", "args": ["--flag"]},
        },
    }
    desktop_config = {"theme": "light", "mcpServers": {}}
    _write_json(claude_code, code_config)
    _write_json(claude_desktop, desktop_config)

    assert setup.install_windows_clients(profile) == [claude_code, claude_desktop]

    entry = {
        "command": "wsl.exe",
        "args": ["-e", *setup.command_prefix(), "serve"],
    }
    installed_code = json.loads(claude_code.read_text(encoding="utf-8"))
    installed_desktop = json.loads(claude_desktop.read_text(encoding="utf-8"))
    assert installed_code["mcpServers"]["khipumaq"] == entry
    assert installed_desktop["mcpServers"]["khipumaq"] == entry
    assert installed_code["theme"] == "dark"
    assert installed_code["mcpServers"]["other"] == code_config["mcpServers"][
        "other"
    ]
    assert installed_desktop["theme"] == "light"

    setup.uninstall_windows_clients(profile)

    assert json.loads(claude_code.read_text(encoding="utf-8")) == code_config
    assert json.loads(claude_desktop.read_text(encoding="utf-8")) == desktop_config


def test_install_windows_clients_does_not_create_missing_config(tmp_path):
    profile = tmp_path / "windows-profile"
    paths = setup.windows_client_configs(profile)

    assert setup.install_windows_clients(profile) == []
    assert all(not path.exists() for path in paths)
