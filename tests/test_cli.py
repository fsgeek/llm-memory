from khipumaq import cli, db, setup


def _isolate_install(monkeypatch, tmp_path, install_codex):
    calls = []
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    settings = tmp_path / "home" / ".claude" / "settings.json"
    claude_json = tmp_path / "home" / ".claude.json"
    monkeypatch.setattr(cli, "_claude_paths", lambda: (settings, claude_json))
    monkeypatch.setattr(
        setup,
        "install_claude",
        lambda *paths: calls.append(("claude", paths)),
    )
    monkeypatch.setattr(setup, "codex_home", lambda: codex_home)
    monkeypatch.setattr(setup, "codex_binary", lambda: "/tmp/fake-codex")
    monkeypatch.setattr(setup, "install_codex", install_codex(calls))
    monkeypatch.setattr(
        setup,
        "has_systemd_user",
        lambda: calls.append(("systemd",)) or True,
    )
    monkeypatch.setattr(
        setup,
        "install_timer",
        lambda: calls.append(("timer",)),
    )
    monkeypatch.setattr(db, "config_path", lambda: tmp_path / "db-config.ini")
    return calls


def test_install_returns_one_and_reaches_timer_when_codex_trust_fails(
    tmp_path, monkeypatch, capsys
):
    def failing_codex(calls):
        def install(home, binary):
            calls.append(("codex", home, binary))
            raise RuntimeError("boom")

        return install

    calls = _isolate_install(monkeypatch, tmp_path, failing_codex)

    assert cli.main(["install"]) == 1

    assert ("timer",) in calls
    assert calls.index(("timer",)) > next(
        index for index, call in enumerate(calls) if call[0] == "codex"
    )
    assert "Codex hooks NOT trusted: boom" in capsys.readouterr().err


def test_install_returns_zero_when_all_steps_succeed(tmp_path, monkeypatch):
    def successful_codex(calls):
        return lambda home, binary: calls.append(("codex", home, binary))

    calls = _isolate_install(monkeypatch, tmp_path, successful_codex)

    assert cli.main(["install"]) == 0

    assert [call[0] for call in calls] == ["claude", "codex", "systemd", "timer"]
