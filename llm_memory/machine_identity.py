from __future__ import annotations

import uuid
from pathlib import Path


def normalize_machine_uuid(raw: str) -> str:
    if not isinstance(raw, str):
        raise TypeError("machine UUID must be a string")
    try:
        value = uuid.UUID(raw.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("machine UUID must be a valid UUID") from exc
    if value.int == 0:
        raise ValueError("machine UUID must not be nil")
    return str(value)


def linux_machine_uuid(path: Path = Path("/etc/machine-id")) -> str:
    return normalize_machine_uuid(path.read_text(encoding="utf-8"))
