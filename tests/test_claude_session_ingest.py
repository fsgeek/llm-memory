import io
import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from khipumaq.db import get_database
from khipumaq.index import EPISODES, ensure_index
from khipumaq.ingest import (
    _turn_text,
    claude_session_files,
    claude_session_to_episodes,
    ingest_claude_session,
    label_from_project_dir,
    main,
    read_machine_id,
)


def _user_record(session_id, text, timestamp="2026-09-02T10:00:00Z"):
    return {
        "type": "user",
        "sessionId": session_id,
        "timestamp": timestamp,
        "message": {"content": text},
    }


def _assistant_record(
    session_id,
    assistant_uuid,
    content,
    timestamp="2026-09-02T10:00:01Z",
    agent_id=None,
):
    record = {
        "type": "assistant",
        "sessionId": session_id,
        "uuid": assistant_uuid,
        "timestamp": timestamp,
        "message": {"model": "claude-test", "content": content},
    }
    if agent_id is not None:
        record["agentId"] = agent_id
    return record


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _delete_if_present(collection, keys):
    for key in keys:
        if collection.has(key):
            collection.delete(key)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("plain message", "plain message"),
        (
            [
                {"type": "text", "text": "first block"},
                {"type": "tool_use", "name": "Read"},
                "not a content block",
                {"type": "text", "text": "second block"},
            ],
            "first block second block",
        ),
        (None, ""),
        ({"type": "text", "text": "not wrapped in a list"}, ""),
        (42, ""),
    ],
)
def test_turn_text_extracts_only_supported_text_content(content, expected):
    assert _turn_text(content) == expected


@pytest.mark.parametrize(
    ("project_dir", "expected"),
    [
        ("hamutay", "hamutay"),
        ("-home-tony-projects-hamutay", "hamutay"),
        ("-home-tony-projects-hamutay--worktrees-recall", "hamutay"),
        ("-home-tony-projects-hamutay-.worktrees-recall", "hamutay"),
        ("-home-tony-projects-hamutay-.claude-worktrees-recall", "hamutay"),
        ("-home-tony-projects-wamason.com", "wamason-com"),
        ("-home-tony-projects-yanantin", "yanantin_construction"),
        ("-home-tony-projects-quantumos", "quantumos"),
        ("-home-tony", "home"),
    ],
)
def test_label_from_project_dir_normalizes_project_layouts(project_dir, expected):
    assert label_from_project_dir(project_dir) == expected


def test_label_from_project_dir_recovers_scratchpad_project():
    scratch_uuid = str(uuid4())
    project_dir = (
        "-tmp-claude-1000--home-tony-projects-hamutay-"
        f"{scratch_uuid}-scratchpad-design-notes"
    )

    assert label_from_project_dir(project_dir) == "hamutay"


def test_claude_session_to_episodes_maps_identity_provenance_and_latest_user(tmp_path):
    session_id = str(uuid4())
    tool_only_uuid = str(uuid4())
    prose_uuid = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    path = tmp_path / "session.jsonl"
    lines = [
        json.dumps(_user_record(session_id, "superseded question")),
        "this is not json",
        json.dumps({"type": "assistant", "message": "not an object"}),
        json.dumps(
            _user_record(
                session_id,
                [{"type": "text", "text": "most recent question"}],
                "2026-09-02T10:00:02Z",
            )
        ),
        json.dumps(
            _assistant_record(
                session_id,
                tool_only_uuid,
                [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/example"},
                    }
                ],
                agent_id=agent_id,
            )
        ),
        json.dumps(
            _assistant_record(
                session_id,
                prose_uuid,
                [
                    {"type": "tool_use", "id": "tool-2", "name": "Read", "input": {}},
                    {"type": "text", "text": "answer after tool use"},
                ],
                "2026-09-02T10:00:03Z",
                agent_id,
            )
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    episodes = list(
        claude_session_to_episodes(
            path,
            "hamutay",
            host="origin-host",
            machine_id="origin-machine-id",
        )
    )

    assert len(episodes) == 1
    episode = episodes[0]
    assert episode["_key"] == prose_uuid
    assert episode["session_id"] == session_id
    assert episode["user_message"] == "most recent question"
    assert episode["user_ts"] == "2026-09-02T10:00:02Z"
    assert episode["response"] == "answer after tool use"
    assert episode["source_file"] == str(path)
    assert episode["host"] == "origin-host"
    assert episode["machine_id"] == "origin-machine-id"
    assert episode["agent_id"] == agent_id
    assert all(episode["_key"] != tool_only_uuid for episode in episodes)


def test_forked_sessions_dedupe_shared_uuid_and_keep_both_tails(tmp_path):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    first_session = str(uuid4())
    resumed_session = str(uuid4())
    shared_uuids = [str(uuid4()), str(uuid4())]
    first_tail_uuid = str(uuid4())
    resumed_tail_uuid = str(uuid4())
    shared_responses = [
        f"first shared response marker {uuid4()}",
        f"second shared response marker {uuid4()}",
    ]
    first_path = tmp_path / "first.jsonl"
    resumed_path = tmp_path / "resumed.jsonl"
    keys_to_clean = {
        *shared_uuids,
        first_tail_uuid,
        resumed_tail_uuid,
        f"{first_session}-{first_tail_uuid}",
        f"{resumed_session}-{resumed_tail_uuid}",
        *(f"{session}-{assistant_uuid}" for session in (first_session, resumed_session)
          for assistant_uuid in shared_uuids),
    }
    try:
        _write_jsonl(
            first_path,
            [
                _user_record(first_session, "shared prompt"),
                _assistant_record(first_session, shared_uuids[0], shared_responses[0]),
                _user_record(first_session, "shared follow-up", "2026-09-02T10:00:02Z"),
                _assistant_record(
                    first_session,
                    shared_uuids[1],
                    shared_responses[1],
                    "2026-09-02T10:00:03Z",
                ),
                _user_record(first_session, "first branch prompt", "2026-09-02T10:00:04Z"),
                _assistant_record(
                    first_session,
                    first_tail_uuid,
                    "first branch tail",
                    "2026-09-02T10:00:05Z",
                ),
            ],
        )
        _write_jsonl(
            resumed_path,
            [
                _user_record(resumed_session, "shared prompt"),
                _assistant_record(resumed_session, shared_uuids[0], shared_responses[0]),
                _user_record(resumed_session, "shared follow-up", "2026-09-02T10:00:02Z"),
                _assistant_record(
                    resumed_session,
                    shared_uuids[1],
                    shared_responses[1],
                    "2026-09-02T10:00:03Z",
                ),
                _user_record(resumed_session, "resumed branch prompt", "2026-09-02T10:00:06Z"),
                _assistant_record(
                    resumed_session,
                    resumed_tail_uuid,
                    "resumed branch tail",
                    "2026-09-02T10:00:07Z",
                ),
            ],
        )

        assert ingest_claude_session(db, first_path, "hamutay") == 3
        assert ingest_claude_session(db, resumed_path, "hamutay") == 3

        shared_documents = list(
            db.aql.execute(
                "FOR episode IN episodes "
                "FILTER episode.response IN @responses "
                "RETURN episode",
                bind_vars={"responses": shared_responses},
            )
        )
        assert len(shared_documents) == 2
        assert {episode["_key"] for episode in shared_documents} == set(shared_uuids)
        assert all(episode["session_id"] == resumed_session for episode in shared_documents)
        assert all(episode["source_file"] == str(resumed_path) for episode in shared_documents)
        assert collection.get(first_tail_uuid)["response"] == "first branch tail"
        assert collection.get(resumed_tail_uuid)["response"] == "resumed branch tail"
    finally:
        _delete_if_present(collection, keys_to_clean)


def test_session_file_and_subagent_are_both_ingested(tmp_path):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    session_id = str(uuid4())
    parent_uuid = str(uuid4())
    subagent_uuid = str(uuid4())
    agent_id = f"agent-{uuid4()}"
    parent = tmp_path / "project" / f"{session_id}.jsonl"
    subagent = parent.with_suffix("") / "subagents" / "agent-x.jsonl"
    keys_to_clean = {
        parent_uuid,
        subagent_uuid,
        f"{session_id}-{parent_uuid}",
        f"{session_id}-{subagent_uuid}",
    }
    try:
        _write_jsonl(
            parent,
            [
                _user_record(session_id, "parent prompt"),
                _assistant_record(session_id, parent_uuid, "parent response"),
            ],
        )
        _write_jsonl(
            subagent,
            [
                _user_record(session_id, "subagent prompt"),
                _assistant_record(
                    session_id,
                    subagent_uuid,
                    "subagent response",
                    agent_id=agent_id,
                ),
            ],
        )

        assert claude_session_files(parent) == [parent, subagent]
        assert ingest_claude_session(
            db,
            parent,
            "hamutay",
            host="parent-host",
            machine_id="parent-machine",
        ) == 2

        parent_episode = collection.get(parent_uuid)
        subagent_episode = collection.get(subagent_uuid)
        assert parent_episode["response"] == "parent response"
        assert parent_episode["agent_id"] is None
        assert parent_episode["source_file"] == str(parent)
        assert subagent_episode["response"] == "subagent response"
        assert subagent_episode["agent_id"] == agent_id
        assert subagent_episode["source_file"] == str(subagent)
        assert subagent_episode["host"] == "parent-host"
        assert subagent_episode["machine_id"] == "parent-machine"
    finally:
        _delete_if_present(collection, keys_to_clean)


def test_main_path_dry_run_reports_count_without_writing(
    tmp_path, monkeypatch, capsys
):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    session_id = str(uuid4())
    assistant_uuid = str(uuid4())
    path = tmp_path / "-home-tony-projects-hamutay" / f"{session_id}.jsonl"
    event_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(event_log))
    keys_to_clean = {assistant_uuid, f"{session_id}-{assistant_uuid}"}
    try:
        _write_jsonl(
            path,
            [
                _user_record(session_id, "dry-run prompt"),
                _assistant_record(session_id, assistant_uuid, "dry-run response"),
            ],
        )

        result = main(
            [
                "claude-session",
                str(path),
                "--dry-run",
                "--host",
                "dry-run-host",
                "--machine-id",
                "dry-run-machine",
            ]
        )

        captured = capsys.readouterr()
        assert result == 0
        assert "would ingest 1" in captured.out
        assert not collection.has(assistant_uuid)
        assert not collection.has(f"{session_id}-{assistant_uuid}")
        assert not event_log.exists()
    finally:
        _delete_if_present(collection, keys_to_clean)


def test_claude_session_cli_emits_one_completed_ingest_event(tmp_path, monkeypatch):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    session_id = str(uuid4())
    assistant_uuid = str(uuid4())
    path = tmp_path / "-home-tony-projects-hamutay" / f"{session_id}.jsonl"
    event_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("LLM_MEMORY_EVENT_LOG", str(event_log))
    keys_to_clean = {assistant_uuid, f"{session_id}-{assistant_uuid}"}
    try:
        _write_jsonl(
            path,
            [
                _user_record(session_id, "event prompt"),
                _assistant_record(session_id, assistant_uuid, "event response"),
            ],
        )

        result = main(
            [
                "claude-session",
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
            "kind": "claude-session",
            "label": "event-label",
            "source_file": str(path),
            "ts": records[0]["ts"],
        }
    finally:
        _delete_if_present(collection, keys_to_clean)


def test_main_hook_mode_reads_transcript_path_from_stdin(tmp_path, monkeypatch):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    session_id = str(uuid4())
    assistant_uuid = str(uuid4())
    path = tmp_path / "-home-tony-projects-hamutay" / f"{session_id}.jsonl"
    keys_to_clean = {assistant_uuid, f"{session_id}-{assistant_uuid}"}
    try:
        _write_jsonl(
            path,
            [
                _user_record(session_id, "hook prompt"),
                _assistant_record(session_id, assistant_uuid, "hook response"),
            ],
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(json.dumps({"transcript_path": str(path)})),
        )

        result = main(
            [
                "claude-session",
                "--host",
                "hook-host",
                "--machine-id",
                "hook-machine",
            ]
        )

        assert result == 0
        episode = collection.get(assistant_uuid)
        assert episode["response"] == "hook response"
        assert episode["experiment_label"] == "hamutay"
        assert episode["host"] == "hook-host"
        assert episode["machine_id"] == "hook-machine"
    finally:
        _delete_if_present(collection, keys_to_clean)


def test_installed_hook_command_imports_from_foreign_cwd_without_pythonpath(
    tmp_path,
):
    git_root = Path(__file__).resolve().parents[1]
    khipumaq = git_root / ".venv" / "bin" / "khipumaq"
    command = [
        str(khipumaq),
        "ingest",
        "claude-session",
        "--dry-run",
    ]
    session_id = str(uuid4())
    transcript = tmp_path / "-home-tony-projects-hamutay" / f"{session_id}.jsonl"
    _write_jsonl(
        transcript,
        [
            _user_record(session_id, "foreign cwd hook prompt"),
            _assistant_record(
                session_id,
                str(uuid4()),
                "foreign cwd hook response",
            ),
        ],
    )
    foreign_cwd = tmp_path / "foreign-project"
    foreign_cwd.mkdir()
    hook_input = json.dumps({"transcript_path": str(transcript)})

    hook_env = os.environ.copy()
    hook_env.pop("PYTHONPATH", None)
    hook_env.update(
        {
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "CODEX_HOME": str(tmp_path / "codex"),
            "KHIPUMAQ_CONFIG": str(git_root / "config" / "db-config.ini"),
        }
    )
    completed = subprocess.run(
        command,
        cwd=foreign_cwd,
        env=hook_env,
        input=hook_input,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "would ingest 1" in completed.stdout


def test_main_missing_transcript_returns_two_and_reports_path(tmp_path, capsys):
    missing = tmp_path / "missing.jsonl"

    result = main(["claude-session", str(missing)])

    captured = capsys.readouterr()
    assert result == 2
    assert "transcript not found" in captured.err
    assert str(missing) in captured.err


def test_main_label_option_overrides_derived_label(tmp_path):
    db = get_database()
    ensure_index(db)
    collection = db.collection(EPISODES)
    session_id = str(uuid4())
    assistant_uuid = str(uuid4())
    override = f"manual-{uuid4().hex}"
    path = tmp_path / "-home-tony-projects-yanantin" / f"{session_id}.jsonl"
    keys_to_clean = {assistant_uuid, f"{session_id}-{assistant_uuid}"}
    try:
        _write_jsonl(
            path,
            [
                _user_record(session_id, "override prompt"),
                _assistant_record(session_id, assistant_uuid, "override response"),
            ],
        )

        result = main(
            [
                "claude-session",
                str(path),
                "--label",
                override,
                "--host",
                "override-host",
                "--machine-id",
                "override-machine",
            ]
        )

        assert result == 0
        assert collection.get(assistant_uuid)["experiment_label"] == override
    finally:
        _delete_if_present(collection, keys_to_clean)


def test_read_machine_id_reads_and_strips_given_path(tmp_path):
    machine_id_path = tmp_path / "machine-id"
    machine_id_path.write_text("  machine-id-with-whitespace  \n", encoding="utf-8")

    assert read_machine_id(machine_id_path) == "machine-id-with-whitespace"
