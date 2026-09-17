"""Append-only JSONL audit log shared by the CLI, HTTP and MCP paths.

One line per event. Every answer is logged today with the principal, the entitlement predicate
applied (None until filtering lands) and the chunk ids returned. Retrieval through the HTTP and
MCP paths will log the same way, so the two enforcement layers can be checked against each
other after the fact.

One process appends at a time today. The HTTP and MCP servers will need a lock or a single
writer before they share this file.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sovrag.config import get_settings


def write_event(event: str, **fields: object) -> None:
    target = Path(get_settings().audit_log_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
