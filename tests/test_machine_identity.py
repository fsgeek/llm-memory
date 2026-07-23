from pathlib import Path

import pytest

from llm_memory.machine_identity import linux_machine_uuid, normalize_machine_uuid


def test_normalizes_platform_identifier_to_canonical_uuid():
    assert normalize_machine_uuid("E8C598AE711B42B5B963EB35FC946D2B\n") == (
        "e8c598ae-711b-42b5-b963-eb35fc946d2b"
    )


@pytest.mark.parametrize("raw", ["", "not-a-uuid", "00000000000000000000000000000000"])
def test_rejects_missing_malformed_and_nil_identifiers(raw):
    with pytest.raises(ValueError, match="machine UUID"):
        normalize_machine_uuid(raw)


def test_linux_collector_reads_then_normalizes(tmp_path):
    machine_id = tmp_path / "machine-id"
    machine_id.write_text("E8C598AE711B42B5B963EB35FC946D2B\n", encoding="utf-8")

    assert linux_machine_uuid(machine_id) == (
        "e8c598ae-711b-42b5-b963-eb35fc946d2b"
    )


def test_linux_collector_does_not_fall_back_when_file_is_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        linux_machine_uuid(tmp_path / "missing-machine-id")
