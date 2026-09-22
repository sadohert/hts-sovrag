"""Append-only JSONL audit log shared by the CLI, HTTP and MCP paths.

One line per event. Every answer is logged with the principal, the entitlement predicate
applied and the chunk ids returned; the MCP server logs each resolution and search the same
way, so the two enforcement layers can be checked against each other after the fact.

Each call takes an exclusive advisory lock (fcntl.flock) on the file for its one write, so
concurrent handlers in one process, or several processes sharing the file, never tear or
interleave a line. flock is POSIX; the image and the developer machines are Linux and macOS.
"""

from __future__ import annotations

import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path

from sovrag.config import get_settings


def write_event(event: str, **fields: object) -> None:
    target = Path(get_settings().audit_log_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record: dict[str, object] = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    with target.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
