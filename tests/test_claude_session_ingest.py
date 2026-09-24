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
        ("C--Users-u-source-repos-hamutay", "hamutay"),
        ("C--Users-u-Documents-Claude-Projects-Gig-Work", "Gig-Work"),
        ("C--Users-u-local-agent-mode-sessions-task-project", "cowork"),
        ("C--Users-u-Documents-Codex-2026-08-25-go", "codex"),
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


def test_claude_session_keeps_prompt_across_tool_round_trip(tmp_path):
    session_id = str(uuid4())
    prompt_ts = "2026-09-24T10:00:00Z"
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            _user_record(session_id, "original prompt", prompt_ts),
            _assistant_record(
                session_id,
                "first-prose",
                "I will inspect the file.",
                "2026-09-24T10:00:01Z",
            ),
            _assistant_record(
                session_id,
                "tool-use-only",
                [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/example"},
                    }
                ],
                "2026-09-24T10:00:02Z",
            ),
            _user_record(
                session_id,
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "file contents",
                    }
                ],
                "2026-09-24T10:00:03Z",
            ),
            _assistant_record(
                session_id,
                "second-prose",
                "The file contains the answer.",
                "2026-09-24T10:00:04Z",
            ),
        ],
    )

    episodes = list(claude_session_to_episodes(path, "hamutay"))

    assert [episode["_key"] for episode in episodes] == [
        "first-prose",
        "second-prose",
    ]
    assert [episode["user_message"] for episode in episodes] == [
        "original prompt",
        "original prompt",
    ]
    assert [episode["user_ts"] for episode in episodes] == [prompt_ts, prompt_ts]


@pytest.mark.parametrize(
    ("ignored_content", "record_flags"),
    [
        pytest.param(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "tool output",
                }
            ],
            {},
            id="tool-result-only",
        ),
        pytest.param("meta text", {"isMeta": True}, id="is-meta"),
        pytest.param(
            "compact summary text",
            {"isCompactSummary": True},
            id="is-compact-summary",
        ),
        pytest.param(
            "This session is being continued from a previous conversation "
            "that ran out of context.",
            {},
            id="continued-session",
        ),
        pytest.param(
            "  This session is being continued from a previous conversation.",
            {},
            id="continued-session-leading-whitespace",
        ),
        pytest.param(
            "<system-reminder>injected</system-reminder>",
            {},
            id="system-reminder",
        ),
        pytest.param(
            "  <system-reminder>injected</system-reminder>",
            {},
            id="system-reminder-leading-whitespace",
        ),
        pytest.param(
            "<local-command-caveat>injected</local-command-caveat>",
            {},
            id="local-command-caveat",
        ),
        pytest.param(
            "\t<local-command-caveat>injected</local-command-caveat>",
            {},
            id="local-command-caveat-leading-whitespace",
        ),
        pytest.param(
            "<local-command-stdout>injected</local-command-stdout>",
            {},
            id="local-command-stdout",
        ),
        pytest.param(
            "\n<local-command-stdout>injected</local-command-stdout>",
            {},
            id="local-command-stdout-leading-whitespace",
        ),
        pytest.param(
            "<local-command-stderr>injected</local-command-stderr>",
            {},
            id="local-command-stderr",
        ),
        pytest.param(
            "  <local-command-stderr>injected</local-command-stderr>",
            {},
            id="local-command-stderr-leading-whitespace",
        ),
        pytest.param(
            "<bash-stdout>injected</bash-stdout>",
            {},
            id="bash-stdout",
        ),
        pytest.param(
            "\t<bash-stdout>injected</bash-stdout>",
            {},
            id="bash-stdout-leading-whitespace",
        ),
        pytest.param(
            "<bash-stderr>injected</bash-stderr>",
            {},
            id="bash-stderr",
        ),
        pytest.param(
            "\n<bash-stderr>injected</bash-stderr>",
            {},
            id="bash-stderr-leading-whitespace",
        ),
        pytest.param(
            "<task-notification>injected</task-notification>",
            {},
            id="task-notification",
        ),
        pytest.param(
            "  <task-notification>injected</task-notification>",
            {},
            id="task-notification-leading-whitespace",
        ),
    ],
)
def test_claude_session_ignores_non_prompt_user_records(
    tmp_path, ignored_content, record_flags
):
    session_id = str(uuid4())
    prompt_ts = "2026-09-24T11:00:00Z"
    ignored = _user_record(
        session_id,
        ignored_content,
        "2026-09-24T11:00:01Z",
    )
    ignored.update(record_flags)
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            _user_record(session_id, "human prompt", prompt_ts),
            ignored,
            _assistant_record(session_id, "answer", "assistant response"),
        ],
    )

    [episode] = list(claude_session_to_episodes(path, "hamutay"))

    assert episode["user_message"] == "human prompt"
    assert episode["user_ts"] == prompt_ts


@pytest.mark.parametrize(
    ("prompt_content", "expected_prompt"),
    [
        pytest.param("plain replacement", "plain replacement", id="plain-string"),
        pytest.param(
            [{"type": "text", "text": "text block replacement"}],
            "text block replacement",
            id="text-block-list",
        ),
        pytest.param(
            [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "aW1hZ2U=",
                    },
                },
                {"type": "text", "text": "describe this image"},
            ],
            "describe this image",
            id="text-alongside-image",
        ),
        pytest.param(
            "<command-name>/review</command-name>",
            "<command-name>/review</command-name>",
            id="command-name",
        ),
        pytest.param(
            "<bash-input>uv run pytest -q</bash-input>",
            "<bash-input>uv run pytest -q</bash-input>",
            id="bash-input",
        ),
        pytest.param(
            '<pasted_content source="clipboard">notes</pasted_content>',
            '<pasted_content source="clipboard">notes</pasted_content>',
            id="pasted-content",
        ),
        pytest.param(
            '<teammate-message teammate_id="reviewer">findings</teammate-message>',
            '<teammate-message teammate_id="reviewer">findings</teammate-message>',
            id="teammate-message",
        ),
    ],
)
def test_claude_session_accepts_person_authored_prompt_shapes(
    tmp_path, prompt_content, expected_prompt
):
    session_id = str(uuid4())
    replacement_ts = "2026-09-24T12:00:01Z"
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            _user_record(session_id, "superseded prompt", "2026-09-24T12:00:00Z"),
            _user_record(session_id, prompt_content, replacement_ts),
            _assistant_record(session_id, "answer", "assistant response"),
        ],
    )

    [episode] = list(claude_session_to_episodes(path, "hamutay"))

    assert episode["user_message"] == expected_prompt
    assert episode["user_ts"] == replacement_ts


def test_claude_session_with_tool_result_before_any_prompt_uses_empty_prompt(tmp_path):
    session_id = str(uuid4())
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [
            _user_record(
                session_id,
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "tool output",
                    }
                ],
            ),
            _assistant_record(session_id, "answer", "assistant response"),
        ],
    )

    [episode] = list(claude_session_to_episodes(path, "hamutay"))

    assert episode["user_message"] == ""
    assert episode["user_ts"] is None


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
