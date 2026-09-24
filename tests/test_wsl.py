from khipumaq import wsl


def test_windows_path_converts_mounted_drive_path():
    assert wsl.windows_path("/mnt/c/Users/u/.codex/x.jsonl") == (
        r"C:\Users\u\.codex\x.jsonl"
    )


def test_windows_path_leaves_non_mounted_path_unchanged():
    path = "/home/u/.codex/x.jsonl"

    assert wsl.windows_path(path) == path


def test_sources_include_profile_clients_and_each_cowork_projects_tree(tmp_path):
    profile = tmp_path / "windows-profile"
    cowork = (
        profile
        / "AppData"
        / "Roaming"
        / "Claude"
        / "local-agent-mode-sessions"
    )
    projects = [
        cowork / "task-b" / "outputs" / "work" / ".claude" / "projects",
        cowork / "task-a" / ".claude" / "projects",
    ]
    for path in projects:
        path.mkdir(parents=True)

    expected = [
        (profile / ".claude" / "projects", profile / ".codex" / "sessions", None)
    ]
    expected.extend((path, None, "cowork") for path in sorted(projects))

    assert list(wsl.sources(profile)) == expected


def test_windows_profile_returns_none_without_wsl_interop(monkeypatch):
    monkeypatch.setattr(wsl.Path, "exists", lambda path: False)
    monkeypatch.setattr(
        wsl,
        "_run",
        lambda *argv: (_ for _ in ()).throw(
            AssertionError("Windows interop must not be invoked")
        ),
    )

    assert wsl.windows_profile() is None
