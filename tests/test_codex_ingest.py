import io
import json
import os
import sys
from uuid import uuid4

import pytest

from khipumaq.db import get_database
from khipumaq.index import EPISODES, ensure_index
from khipumaq.ingest import (
    codex_rollout_files,
    codex_rollout_to_episodes,
    ingest_codex_rollout,
    label_from_path,
    main,
    sweep,
)


def _session_meta(
    session_id,
    timestamp="2026-09-03T10:00:00Z",
    *,
    cwd="/home/tony/projects/hamutay",
    originator="codex-tui",
    cli_version="0.151.0",
    forked_from_id=None,
    agent_nickname=None,
    thread_source=None,
):
    payload = {
        "id": session_id,
        "originator": originator,
        "cli_version": cli_version,
    }
    if cwd is not None:
        payload["cwd"] = cwd
    if forked_from_id is not None:
        payload["forked_from_id"] = forked_from_id
    if agent_nickname is not None:
        payload["agent_nickname"] = agent_nickname
    if thread_source is not None:
        payload["thread_source"] = thread_source
    return {"timestamp": timestamp, "type": "session_meta", "payload": payload}


def _turn_context(model, timestamp="2026-09-03T10:00:01Z"):
    return {
        "timestamp": timestamp,
        "type": "turn_context",
        "payload": {"model": model},
    }


def _message(role, text, timestamp, message_id=None):
    block_type = "input_text" if role == "user" else "output_text"
    payload = {
        "type": "message",
        "role": role,
        "content": [{"type": block_type, "text": text}],
    }
    if message_id is not None:
        payload["id"] = message_id
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [record if isinstance(record, str) else json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _delete_if_present(collection, keys):
    for key in keys:
        if collection.has(key):
            collection.delete(key)


def _without_revision(document):
    return {key: value for key, value in document.items() if key != "_rev"}


def test_codex_rollout_maps_prose_messages_with_identity_and_provenance(tmp_path):
    session_id = f"codex-test-{uuid4().hex}"
    message_id = f"msg_{uuid4().hex}"
    path = tmp_path / "rollout-map.jsonl"
    records = [
        _session_meta(
            session_id,
            cwd="/home/tony/projects/hamutay",
            originator="Claude Code",
            cli_version="0.150.1",
            agent_nickname="Noether",
            thread_source="subagent",
        ),
        _turn_context("gpt-5.5"),
        _message("user", "superseded question", "2026-09-03T10:00:02Z"),
        _message("assistant", "", "2026-09-03T10:00:03Z", "msg_empty"),
        _message("assistant", "  \n", "2026-09-03T10:00:04Z", "msg_whitespace"),
        _message("user", "most recent question", "2026-09-03T10:00:05Z"),
        _message("assistant", "first prose answer", "2026-09-03T10:00:06Z", message_id),
        _turn_context("gpt-5.6-sol", "2026-09-03T10:00:07Z"),
        _message("assistant", "second prose answer", "2026-09-03T10:00:08Z"),
    ]
    _write_jsonl(path, records)

    episodes = list(
        codex_rollout_to_episodes(
            path,
            host="origin-host",
            machine_id="origin-machine-id",
        )
    )

    assert len(episodes) == 2
    first, second = episodes
    assert first["_key"] == f"{session_id}-{message_id}"
    assert second["_key"] == f"{session_id}-line8"
    assert [episode["response"] for episode in episodes] == [
        "first prose answer",
        "second prose answer",
    ]
    assert [episode["model"] for episode in episodes] == ["gpt-5.5", "gpt-5.6-sol"]
    assert all(episode["session_id"] == session_id for episode in episodes)
    assert all(episode["user_message"] == "most recent question" for episode in episodes)
    assert all(episode["user_ts"] == "2026-09-03T10:00:05Z" for episode in episodes)
    assert all(episode["source_file"] == str(path) for episode in episodes)
    assert all(episode["host"] == "origin-host" for episode in episodes)
    assert all(episode["machine_id"] == "origin-machine-id" for episode in episodes)
    assert first["ts"] == "2026-09-03T10:00:06Z"
    assert first["codex"] == {
        "originator": "Claude Code",
        "cli_version": "0.150.1",
        "cwd": "/home/tony/projects/hamutay",
        "thread_source": "subagent",
        "agent_nickname": "Noether",
        "forked_from_id": None,
    }


@pytest.mark.parametrize(
    "injected",
    [
        "<environment_context>environment details</environment_context>",
        '<codex_internal_context source="goal">stored goal</codex_internal_context>',
        "<recommended_plugins>plugin list</recommended_plugins>",
        "<turn_aborted>aborted turn</turn_aborted>",
        "<user_instructions>harness rules</user_instructions>",
        "<subagent_notification>child result</subagent_notification>",
        "# AGENTS.md instructions for /home/tony/projects/hamutay",
        "Warning: apply_patch was requested via exec_command",
    ],
)
def test_injected_user_messages_do_not_replace_the_latest_prompt(tmp_path, injected):
    session_id = f"codex-injected-{uuid4().hex}"
    path = tmp_path / f"rollout-{uuid4().hex}.jsonl"
    _write_jsonl(
        path,
        [
            _session_meta(session_id),
            _turn_context("gpt-5.6-sol"),
            _message("user", "real user prompt", "2026-09-03T10:00:02Z"),
            _message("user", injected, "2026-09-03T10:00:03Z"),
            _message("assistant", "answer", "2026-09-03T10:00:04Z", "msg_answer"),
        ],
    )

    episode = next(codex_rollout_to_episodes(path))

    assert episode["user_message"] == "real user prompt"
    assert episode["user_ts"] == "2026-09-03T10:00:02Z"


def test_task_tag_is_a_prompt_but_injected_context_alone_is_not(tmp_path):
    session_id = f"codex-task-{uuid4().hex}"
    path = tmp_path / "rollout-task.jsonl"
    task = "<task>write independent verification tests</task>"
    _write_jsonl(
        path,
        [
            _session_meta(session_id),
            _turn_context("gpt-5.6-sol"),
            _message(
                "user",
                "<environment_context>details</environment_context>",
                "2026-09-03T10:00:02Z",
            ),
            _message("assistant", "context-only response", "2026-09-03T10:00:03Z", "msg_context"),
            _message("user", task, "2026-09-03T10:00:04Z"),
            _message("assistant", "task response", "2026-09-03T10:00:05Z", "msg_task"),
        ],
    )

    episodes = list(codex_rollout_to_episodes(path))

    assert [episode["user_message"] for episode in episodes] == ["", task]
    assert episodes[0]["user_ts"] is None
    assert episodes[1]["user_ts"] == "2026-09-03T10:00:04Z"


def test_forked_rollout_skips_replay_and_uses_first_session_meta(tmp_path):
    fork_session = f"codex-fork-{uuid4().hex}"
    parent_session = f"codex-parent-{uuid4().hex}"
    path = tmp_path / "rollout-fork.jsonl"
    _write_jsonl(
        path,
        [
            _session_meta(
                fork_session,
                "2026-09-03T10:00:00.000Z",
                originator="codex_exec",
                forked_from_id=parent_session,
                agent_nickname="Helmholtz",
                thread_source="subagent",
            ),
            _session_meta(
                parent_session,
                "2026-09-03T10:00:00.010Z",
                cwd="/home/tony/projects/yanantin",
                originator="parent-originator",
            ),
            _message("user", "replayed prompt", "2026-09-03T10:00:00.020Z"),
            _message("assistant", "replayed answer", "2026-09-03T10:00:00.050Z", "msg_replay"),
            _turn_context("gpt-5.6-sol", "2026-09-03T10:00:02.000Z"),
            _message("user", "live prompt", "2026-09-03T10:00:02.100Z"),
            _message("assistant", "live answer", "2026-09-03T10:00:02.200Z", "msg_live"),
        ],
    )

    episodes = list(codex_rollout_to_episodes(path))

    assert len(episodes) == 1
    assert episodes[0]["_key"] == f"{fork_session}-msg_live"
    assert episodes[0]["user_message"] == "live prompt"
    assert episodes[0]["response"] == "live answer"
    assert episodes[0]["model"] == "gpt-5.6-sol"
    assert episodes[0]["codex"]["originator"] == "codex_exec"
    assert episodes[0]["codex"]["forked_from_id"] == parent_session
    assert episodes[0]["experiment_label"] == "hamutay"


def test_nonforked_rollout_ignores_a_second_session_meta(tmp_path):
    first_session = f"codex-first-{uuid4().hex}"
    second_session = f"codex-second-{uuid4().hex}"
    path = tmp_path / "rollout-two-meta.jsonl"
    _write_jsonl(
        path,
        [
            _session_meta(first_session, originator="first-originator"),
            _session_meta(
                second_session,
                "2026-09-03T10:00:00.100Z",
                originator="second-originator",
            ),
            _turn_context("gpt-5.5"),
            _message("user", "question", "2026-09-03T10:00:02Z"),
            _message("assistant", "answer", "2026-09-03T10:00:03Z", "msg_answer"),
        ],
    )

    episode = next(codex_rollout_to_episodes(path))

    assert episode["session_id"] == first_session
    assert episode["_key"] == f"{first_session}-msg_answer"
    assert episode["codex"]["originator"] == "first-originator"


def test_nonmessage_and_malformed_rows_are_ignored(tmp_path):
    session_id = f"codex-ignore-{uuid4().hex}"
    path = tmp_path / "rollout-ignore.jsonl"
    records = [
        _session_meta(session_id),
        _turn_context("gpt-5.5"),
        _message("user", "real prompt", "2026-09-03T10:00:02Z"),
        {
            "timestamp": "2026-09-03T10:00:03Z",
            "type": "response_item",
            "payload": {"type": "function_call", "name": "search"},
        },
        {
            "timestamp": "2026-09-03T10:00:04Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": []},
        },
        {
            "timestamp": "2026-09-03T10:00:05Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "apply_patch"},
        },
        {
            "timestamp": "2026-09-03T10:00:06Z",
            "type": "event_msg",
            "payload": {"type": "agent_message", "message": "event answer"},
        },
        {
            "timestamp": "2026-09-03T10:00:07Z",
            "type": "compacted",
            "payload": {"replacement_history": [{"role": "user", "text": "replay"}]},
        },
        "this is not parseable json",
        _message("assistant", "real answer", "2026-09-03T10:00:09Z", "msg_real"),
    ]
    _write_jsonl(path, records)

    episodes = list(codex_rollout_to_episodes(path))

    assert len(episodes) == 1
    assert episodes[0]["user_message"] == "real prompt"
    assert episodes[0]["response"] == "real answer"


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/home/tony/projects/hamutay", "hamutay"),
        ("/home/tony/projects/hamutay/.worktrees/turboquant-r1", "hamutay"),
        ("/home/tony/projects/yanantin", "yanantin_construction"),
        ("/home/tony/projects/wamason.com", "wamason-com"),
        (None, "codex"),
    ],
)
def test_codex_rollout_defaults_label_from_cwd(tmp_path, cwd, expected):
    session_id = f"codex-label-{uuid4().hex}"
    path = tmp_path / f"rollout-{uuid4().hex}.jsonl"
    _write_jsonl(
        path,
        [
            _session_meta(session_id, cwd=cwd),
            _turn_context("gpt-5.6-sol"),
            _message("assistant", "label answer", "2026-09-03T10:00:02Z", "msg_label"),
        ],
    )

    episode = next(codex_rollout_to_episodes(path))

    assert episode["experiment_label"] == expected


def test_explicit_codex_label_overrides_cwd(tmp_path):
    session_id = f"codex-override-{uuid4().hex}"
    path = tmp_path / "rollout-override.jsonl"
    _write_jsonl(
        path,
        [
            _session_meta(session_id, cwd="/home/tony/projects/yanantin"),
            _turn_context("gpt-5.6-sol"),
            _message("assistant", "answer", "2026-09-03T10:00:02Z", "msg_label"),
        ],
    )

    episode = next(codex_rollout_to_episodes(path, "manual-label"))

    assert episode["experiment_label"] == "manual-label"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/home/tony/projects/hamutay", "hamutay"),
        ("/home/tony/projects/hamutay/.worktrees/turboquant-r1", "hamutay"),
        ("/home/tony/projects/yanantin", "yanantin_construction"),
        ("/home/tony/projects/wamason.com", "wamason-com"),
        ("/home/tony", "home"),
        ("/home/u/projects/cpsc416/tmp/capstone/s1", "cpsc416"),
        ("/home/u/projects/llm-memory/.worktrees/x", "llm-memory"),
        ("/home/u/projects/yanantin", "yanantin_construction"),
        ("/mnt/c/Users/x/repos/pablo", "mnt-c-users-x-repos-pablo"),
        ("/home/u", "home"),
    ],
)
def test_label_from_path_maps_project_layouts(path, expected):
    assert label_from_path(path) == expected


def test_sweep_ingests_only_fresh_claude_and_codex_files_with_derived_labels(
    tmp_path,
):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    claude_root = tmp_path / "claude-projects"
    codex_root = tmp_path / "codex-sessions"
    fresh_claude_uuid = str(uuid4())
    old_claude_uuid = str(uuid4())
    fresh_claude_session = str(uuid4())
    old_claude_session = str(uuid4())
    fresh_claude = (
        claude_root / "-home-u-projects-demo" / f"{fresh_claude_session}.jsonl"
    )
    old_claude = (
        claude_root / "-home-u-projects-old-demo" / f"{old_claude_session}.jsonl"
    )
    fresh_codex_session = f"sweep-fresh-{marker}"
    old_codex_session = f"sweep-old-{marker}"
    fresh_codex_message = f"msg_fresh_{marker}"
    old_codex_message = f"msg_old_{marker}"
    fresh_codex_key = f"{fresh_codex_session}-{fresh_codex_message}"
    old_codex_key = f"{old_codex_session}-{old_codex_message}"
    fresh_codex = codex_root / "2026" / "09" / "23" / "rollout-fresh.jsonl"
    old_codex = codex_root / "2026" / "09" / "22" / "rollout-old.jsonl"
    keys = {
        fresh_claude_uuid,
        old_claude_uuid,
        fresh_codex_key,
        old_codex_key,
    }
    try:
        _write_jsonl(
            fresh_claude,
            [
                {
                    "type": "user",
                    "sessionId": fresh_claude_session,
                    "timestamp": "2026-09-23T10:00:00Z",
                    "message": {"content": f"fresh Claude prompt {marker}"},
                },
                {
                    "type": "assistant",
                    "sessionId": fresh_claude_session,
                    "uuid": fresh_claude_uuid,
                    "timestamp": "2026-09-23T10:00:01Z",
                    "message": {
                        "model": "claude-test",
                        "content": f"fresh Claude response {marker}",
                    },
                },
            ],
        )
        _write_jsonl(
            old_claude,
            [
                {
                    "type": "assistant",
                    "sessionId": old_claude_session,
                    "uuid": old_claude_uuid,
                    "timestamp": "2026-09-22T10:00:01Z",
                    "message": {
                        "model": "claude-test",
                        "content": f"old Claude response {marker}",
                    },
                }
            ],
        )
        _write_jsonl(
            fresh_codex,
            [
                _session_meta(
                    fresh_codex_session,
                    cwd="/home/u/projects/codex-demo/nested/workdir",
                ),
                _turn_context("gpt-test"),
                _message(
                    "assistant",
                    f"fresh Codex response {marker}",
                    "2026-09-23T10:00:02Z",
                    fresh_codex_message,
                ),
            ],
        )
        _write_jsonl(
            old_codex,
            [
                _session_meta(old_codex_session, cwd="/home/u/projects/old-codex"),
                _turn_context("gpt-test"),
                _message(
                    "assistant",
                    f"old Codex response {marker}",
                    "2026-09-22T10:00:02Z",
                    old_codex_message,
                ),
            ],
        )
        since = 1_800_000_000
        os.utime(fresh_claude, (since + 10, since + 10))
        os.utime(fresh_codex, (since + 10, since + 10))
        os.utime(old_claude, (since - 10, since - 10))
        os.utime(old_codex, (since - 10, since - 10))

        result = sweep(
            db,
            claude_root,
            codex_root,
            since=since,
            host="sweep-host",
            machine_id="sweep-machine",
        )

        assert result == {"claude": (1, 1), "codex": (1, 1)}
        assert collection.get(fresh_claude_uuid)["experiment_label"] == "demo"
        assert collection.get(fresh_codex_key)["experiment_label"] == "codex-demo"
        assert collection.get(fresh_codex_key)["codex"]["cwd"] == (
            "/home/u/projects/codex-demo/nested/workdir"
        )
        assert not collection.has(old_claude_uuid)
        assert not collection.has(old_codex_key)
    finally:
        _delete_if_present(collection, keys)


def test_sweep_counts_missing_roots_as_zero(tmp_path):
    db = get_database()

    assert sweep(db, tmp_path / "missing-claude", tmp_path / "missing-codex") == {
        "claude": (0, 0),
        "codex": (0, 0),
    }


def test_codex_rollout_files_returns_only_dated_rollouts_oldest_first(tmp_path):
    first = tmp_path / "2026" / "09" / "02" / "rollout-a.jsonl"
    second = tmp_path / "2026" / "09" / "03" / "rollout-b.jsonl"
    other = tmp_path / "2026" / "09" / "03" / "other.jsonl"
    notes = tmp_path / "notes.jsonl"
    for path in (first, second, other, notes):
        _write_jsonl(path, [])

    assert codex_rollout_files(tmp_path) == [first, second]


def test_ingest_codex_rollout_is_idempotent_and_dry_run_does_not_write(tmp_path):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    session_id = f"codex-ingest-{marker}"
    dry_session_id = f"codex-dry-{marker}"
    message_ids = [f"msg_{marker}_one", f"msg_{marker}_two"]
    keys = [f"{session_id}-{message_id}" for message_id in message_ids]
    dry_key = f"{dry_session_id}-msg_{marker}_dry"
    path = tmp_path / "rollout-ingest.jsonl"
    dry_path = tmp_path / "rollout-dry.jsonl"
    try:
        _write_jsonl(
            path,
            [
                _session_meta(session_id, originator="Claude Code"),
                _turn_context("gpt-5.6-sol"),
                _message("user", f"prompt {marker}", "2026-09-03T10:00:02Z"),
                _message(
                    "assistant",
                    f"first answer {marker}",
                    "2026-09-03T10:00:03Z",
                    message_ids[0],
                ),
                _message(
                    "assistant",
                    f"second answer {marker}",
                    "2026-09-03T10:00:04Z",
                    message_ids[1],
                ),
            ],
        )
        _write_jsonl(
            dry_path,
            [
                _session_meta(dry_session_id),
                _turn_context("gpt-5.6-sol"),
                _message(
                    "assistant",
                    f"dry answer {marker}",
                    "2026-09-03T10:00:03Z",
                    f"msg_{marker}_dry",
                ),
            ],
        )

        assert ingest_codex_rollout(
            db,
            path,
            host="database-host",
            machine_id="database-machine",
        ) == 2
        first_documents = [_without_revision(collection.get(key)) for key in keys]

        assert first_documents[0]["host"] == "database-host"
        assert first_documents[0]["machine_id"] == "database-machine"
        assert first_documents[0]["codex"]["originator"] == "Claude Code"
        assert first_documents[0]["source_file"] == str(path)

        assert ingest_codex_rollout(
            db,
            path,
            host="database-host",
            machine_id="database-machine",
        ) == 2
        second_documents = [_without_revision(collection.get(key)) for key in keys]
        session_count = next(
            iter(
                db.aql.execute(
                    "RETURN LENGTH(FOR episode IN episodes "
                    "FILTER episode.session_id == @session_id RETURN 1)",
                    bind_vars={"session_id": session_id},
                )
            )
        )

        assert session_count == 2
        assert second_documents == first_documents
        assert ingest_codex_rollout(db, dry_path, dry_run=True) == 1
        assert not collection.has(dry_key)
    finally:
        _delete_if_present(collection, [*keys, dry_key])


def test_codex_cli_ingests_one_path_and_all_rollouts(tmp_path, capsys):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    single_session = f"codex-cli-single-{marker}"
    all_sessions = [f"codex-cli-all-{marker}-{index}" for index in range(2)]
    single_path = tmp_path / "single" / "rollout-single.jsonl"
    root = tmp_path / "sessions"
    all_paths = [
        root / "2026" / "09" / "02" / "rollout-a.jsonl",
        root / "2026" / "09" / "03" / "rollout-b.jsonl",
    ]
    single_key = f"{single_session}-msg_single"
    all_keys = [f"{session}-msg_all_{index}" for index, session in enumerate(all_sessions)]
    try:
        _write_jsonl(
            single_path,
            [
                _session_meta(single_session),
                _turn_context("gpt-5.6-sol"),
                _message("assistant", f"single {marker}", "2026-09-03T10:00:02Z", "msg_single"),
            ],
        )
        for index, (path, session) in enumerate(zip(all_paths, all_sessions)):
            _write_jsonl(
                path,
                [
                    _session_meta(session),
                    _turn_context("gpt-5.6-sol"),
                    _message(
                        "assistant",
                        f"all {index} {marker}",
                        "2026-09-03T10:00:02Z",
                        f"msg_all_{index}",
                    ),
                ],
            )

        result = main(
            ["codex", str(single_path), "--host", "h", "--machine-id", "m"]
        )
        captured = capsys.readouterr()

        assert result == 0
        assert captured.out == (
            f"codex: ingested 1 episodes from {single_path} (host=h)\n"
        )
        assert captured.err == ""
        assert collection.get(single_key)["machine_id"] == "m"

        result = main(
            [
                "codex",
                "--all",
                "--root",
                str(root),
                "--host",
                "h",
                "--machine-id",
                "m",
            ]
        )
        captured = capsys.readouterr()

        assert result == 0
        assert captured.out == (
            f"codex: ingested 2 episodes from 2 files under {root} (host=h)\n"
        )
        assert captured.err == ""
        assert all(collection.has(key) for key in all_keys)
    finally:
        _delete_if_present(collection, [single_key, *all_keys])


def test_codex_cli_session_end_hook_reads_transcript_path_from_stdin(
    tmp_path, monkeypatch, capsys
):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    session_id = f"codex-hook-session-end-{marker}"
    message_id = f"msg_{marker}"
    key = f"{session_id}-{message_id}"
    path = tmp_path / "rollout-session-end.jsonl"
    try:
        _write_jsonl(
            path,
            [
                _session_meta(session_id),
                _turn_context("gpt-5.6-sol"),
                _message(
                    "assistant",
                    f"session end response {marker}",
                    "2026-09-03T10:00:02Z",
                    message_id,
                ),
            ],
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "SessionEnd",
                        "session_id": session_id,
                        "transcript_path": str(path),
                        "cwd": "/home/tony/projects/hamutay",
                    }
                )
            ),
        )

        result = main(["codex", "--host", "h", "--machine-id", "m"])

        captured = capsys.readouterr()
        assert result == 0
        assert captured.out == f"codex: ingested 1 episodes from {path} (host=h)\n"
        assert captured.err == ""
        episode = collection.get(key)
        assert episode["response"] == f"session end response {marker}"
        assert episode["source_file"] == str(path)
        assert episode["host"] == "h"
        assert episode["machine_id"] == "m"
    finally:
        _delete_if_present(collection, [key])


def test_codex_cli_subagent_stop_prefers_agent_transcript_path(
    tmp_path, monkeypatch, capsys
):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    parent_session = f"codex-hook-parent-{marker}"
    agent_session = f"codex-hook-agent-{marker}"
    parent_message = f"msg_parent_{marker}"
    agent_message = f"msg_agent_{marker}"
    parent_key = f"{parent_session}-{parent_message}"
    agent_key = f"{agent_session}-{agent_message}"
    parent_path = tmp_path / "rollout-parent.jsonl"
    agent_path = tmp_path / "rollout-agent.jsonl"
    try:
        _write_jsonl(
            parent_path,
            [
                _session_meta(parent_session),
                _turn_context("gpt-5.6-sol"),
                _message(
                    "assistant",
                    f"parent response {marker}",
                    "2026-09-03T10:00:02Z",
                    parent_message,
                ),
            ],
        )
        _write_jsonl(
            agent_path,
            [
                _session_meta(agent_session),
                _turn_context("gpt-5.6-sol"),
                _message(
                    "assistant",
                    f"agent response {marker}",
                    "2026-09-03T10:00:02Z",
                    agent_message,
                ),
            ],
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "SubagentStop",
                        "session_id": agent_session,
                        "transcript_path": str(parent_path),
                        "agent_transcript_path": str(agent_path),
                        "cwd": "/home/tony/projects/hamutay",
                    }
                )
            ),
        )

        result = main(["codex", "--host", "h", "--machine-id", "m"])

        captured = capsys.readouterr()
        assert result == 0
        assert captured.out == (
            f"codex: ingested 1 episodes from {agent_path} (host=h)\n"
        )
        assert captured.err == ""
        assert collection.get(agent_key)["response"] == f"agent response {marker}"
        assert not collection.has(parent_key)
    finally:
        _delete_if_present(collection, [parent_key, agent_key])


def test_codex_cli_hook_missing_rollout_returns_two_without_writing(
    tmp_path, monkeypatch, capsys
):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    session_id = f"codex-hook-missing-{marker}"
    key = f"{session_id}-msg_{marker}"
    missing = tmp_path / f"rollout-missing-{marker}.jsonl"
    try:
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "SessionEnd",
                        "session_id": session_id,
                        "transcript_path": str(missing),
                        "cwd": "/home/tony/projects/hamutay",
                    }
                )
            ),
        )

        result = main(["codex", "--host", "h", "--machine-id", "m"])

        captured = capsys.readouterr()
        assert result == 2
        assert captured.out == ""
        assert "rollout not found" in captured.err
        assert str(missing) in captured.err
        assert not collection.has(key)
    finally:
        _delete_if_present(collection, [key])


def test_codex_cli_rejects_path_with_all(tmp_path, capsys):
    path = tmp_path / "rollout.jsonl"

    result = main(["codex", str(path), "--all"])

    captured = capsys.readouterr()
    assert result == 2
    assert captured.out == ""
    assert "not both" in captured.err


def test_codex_cli_hook_dry_run_reports_without_writing(
    tmp_path, monkeypatch, capsys
):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    session_id = f"codex-hook-dry-{marker}"
    message_id = f"msg_{marker}"
    key = f"{session_id}-{message_id}"
    path = tmp_path / "rollout-hook-dry.jsonl"
    try:
        _write_jsonl(
            path,
            [
                _session_meta(session_id),
                _turn_context("gpt-5.6-sol"),
                _message(
                    "assistant",
                    f"dry response {marker}",
                    "2026-09-03T10:00:02Z",
                    message_id,
                ),
            ],
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "SessionEnd",
                        "session_id": session_id,
                        "transcript_path": str(path),
                        "cwd": "/home/tony/projects/hamutay",
                    }
                )
            ),
        )

        result = main(
            [
                "codex",
                "--dry-run",
                "--host",
                "h",
                "--machine-id",
                "m",
            ]
        )

        captured = capsys.readouterr()
        assert result == 0
        assert "would ingest" in captured.out
        assert captured.err == ""
        assert not collection.has(key)
    finally:
        _delete_if_present(collection, [key])


def test_codex_cli_emits_one_completed_ingest_event(tmp_path, monkeypatch):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    marker = uuid4().hex
    session_id = f"codex-event-{marker}"
    message_id = f"msg_{marker}"
    key = f"{session_id}-{message_id}"
    path = tmp_path / "rollout-event.jsonl"
    event_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(event_log))
    try:
        _write_jsonl(
            path,
            [
                _session_meta(session_id),
                _turn_context("gpt-5.6-sol"),
                _message(
                    "assistant",
                    f"event response {marker}",
                    "2026-09-03T10:00:02Z",
                    message_id,
                ),
            ],
        )

        result = main(
            [
                "codex",
                str(path),
                "--label",
                "event-label",
                "--host",
                "event-host",
                "--machine-id",
                "event-machine",
            ]
        )

        records = [
            json.loads(line)
            for line in event_log.read_text(encoding="utf-8").splitlines()
        ]
        assert result == 0
        assert len(records) == 1
        assert records[0] == {
            "count": 1,
            "event": "ingest.completed",
            "host": "event-host",
            "kind": "codex",
            "label": "event-label",
            "source_file": str(path),
            "ts": records[0]["ts"],
        }
    finally:
        _delete_if_present(collection, [key])


def test_codex_cli_rejects_missing_path(tmp_path, capsys):
    missing = tmp_path / "missing.jsonl"
    result = main(["codex", str(missing)])
    captured = capsys.readouterr()

    assert result == 2
    assert captured.out == ""
    assert "rollout not found" in captured.err
    assert str(missing) in captured.err
