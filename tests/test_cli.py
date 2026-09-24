from khipumaq import cli, db, index, setup, wsl


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
    monkeypatch.setattr(wsl, "windows_profile", lambda: None)
    monkeypatch.setattr(db, "config_path", lambda: tmp_path / "db-config.ini")
    monkeypatch.setattr(cli, "_ensure_store", lambda: 0)
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


def test_install_returns_one_when_database_config_is_missing(
    tmp_path, monkeypatch, capsys
):
    def successful_codex(calls):
        return lambda home, binary: calls.append(("codex", home, binary))

    _isolate_install(monkeypatch, tmp_path, successful_codex)
    monkeypatch.setattr(
        db,
        "config_path",
        lambda: (_ for _ in ()).throw(FileNotFoundError("missing test config")),
    )
    monkeypatch.setattr(
        cli,
        "_ensure_store",
        lambda: (_ for _ in ()).throw(AssertionError("store must not be opened")),
    )

    assert cli.main(["install"]) == 1

    assert "missing test config" in capsys.readouterr().err


def test_ensure_store_creates_index_for_fresh_database(monkeypatch):
    class FreshDatabase:
        def has_collection(self, name):
            assert name == index.EPISODES
            return False

    fake_db = FreshDatabase()
    indexed = []
    monkeypatch.setattr(db, "get_database", lambda: fake_db)
    monkeypatch.setattr(index, "ensure_index", lambda database: indexed.append(database))

    assert cli._ensure_store() == 0
    assert indexed == [fake_db]


def test_ensure_store_leaves_existing_collection_unchanged(monkeypatch):
    class Collection:
        def count(self):
            return 17

    class ExistingDatabase:
        def has_collection(self, name):
            assert name == index.EPISODES
            return True

        def collection(self, name):
            assert name == index.EPISODES
            return Collection()

    monkeypatch.setattr(db, "get_database", ExistingDatabase)
    monkeypatch.setattr(
        index,
        "ensure_index",
        lambda database: (_ for _ in ()).throw(
            AssertionError("existing store must not be recreated")
        ),
    )

    assert cli._ensure_store() == 0


def test_ensure_store_returns_one_when_database_is_unreachable(monkeypatch, capsys):
    def unreachable():
        raise ConnectionError("test server refused connection")

    monkeypatch.setattr(db, "get_database", unreachable)

    assert cli._ensure_store() == 1
    assert "database unreachable" in capsys.readouterr().err
