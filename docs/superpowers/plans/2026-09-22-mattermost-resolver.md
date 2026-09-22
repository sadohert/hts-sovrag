# Mattermost Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `sovrag.auth.mattermost` turns a Mattermost user ID into a `Principal` by reading admin-managed user attributes over REST, failing closed on anything it cannot verify; and `sovrag.audit` becomes safe for the concurrent writers the MCP server will bring.

**Architecture:** `MattermostResolver(base_url, bot_token)` makes three GET calls per resolution with the bot's bearer token: the user (proves the ID exists and gives the username), the attribute field definitions, and the user's values. Only admin-managed `select`/`multiselect` fields named `sovrag_clearance` and `sovrag_departments` count; option IDs map to names through the definitions fetched in the same call. A known user with nothing recognised is `LEAST_PRIVILEGED` with a note; any failure is `ResolutionError`. `write_event` takes an exclusive advisory lock around each append so lines never tear.

**Tech Stack:** Python 3.14, httpx2 with `MockTransport` in tests (the pattern `tests/test_llm.py` uses), `fcntl.flock`, pydantic-settings, pytest, ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-09-17-mattermost-integration-design.md` (sections "`sovrag.auth.mattermost`", "`sovrag.audit`", "Edge cases", "Tests", and item 4 under "Sequencing"). ADR 0010 is amended by Task 1, because the spec's two-call design cannot detect an unknown user (see Task 1).

## Global Constraints

- A principal is bound by the server from an authenticated transport; identity never comes from tool arguments (CLAUDE.md, ADR 0009). The resolver takes a user ID the caller has already verified and adds its own shape check because the ID goes into a URL path.
- Three outcomes, never blurred (`.claude/rules/retrieval.md`): a failed or unknown lookup is an error, never least privilege; only a known user with nothing recognised is `LEAST_PRIVILEGED`, with the reason recorded.
- The bot token is never logged, never in an exception message, never in a `Resolution`.
- Classification labels and clearances are synthetic (ADR 0003).
- `sovrag.auth` may import `sovrag.config`; it must not import `sovrag.retrieve`, `sovrag.generate` or `sovrag.cli`.
- Type hints everywhere; `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` and `uv run pytest -q` pass before each commit. Run `uv run ruff format docs` before committing this plan or the ADR.
- Tests first. Run the test, watch it fail for the expected reason, then implement.
- Line length 100. No schedule, session or notes-folder references anywhere in the repo.
- End each commit message with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Pin nothing new: httpx2 is already a dependency.

## File Structure

| File | Responsibility |
|---|---|
| `docs/adr/0010-mattermost-attributes-as-entitlement-source.md` (modify) | Amendment: three calls, unknown or deactivated user is an error, username is the principal id |
| `docs/superpowers/specs/2026-09-17-mattermost-integration-design.md` (modify) | Resolver section matches the amendment |
| `src/sovrag/audit.py` (modify) | Exclusive lock around each append |
| `src/sovrag/auth/mattermost.py` (create) | `ResolutionError`, `Resolution`, `MattermostResolver` |
| `src/sovrag/auth/__init__.py` (modify) | Re-export the three names |
| `src/sovrag/config.py` (modify) | `mattermost_url`, `mattermost_bot_token` |
| `.env.example` (modify) | The two settings, documented as mattermost mode only |
| `tests/test_audit.py` (modify) | Concurrent writers never tear a line |
| `tests/test_mattermost_resolver.py` (create) | Recorded responses, every rule, every failure |
| `docs/architecture.md` (modify) | Principals section describes the resolver |

## Branch Setup

Already done: worktree `.claude/worktrees/mattermost-resolver` on branch `mattermost-resolver` from `origin/main` at 8d03d8d. Before Task 1:

```bash
uv sync --all-extras
uv run pytest -q
```

Expected: the suite passes (Postgres tests need the Compose Postgres from the main checkout; they skip if it is down, and nothing in this plan touches the database).

---

### Task 1: Amend ADR 0010 and the spec: three calls, unknown user is an error

**Files:**
- Modify: `docs/adr/0010-mattermost-attributes-as-entitlement-source.md`
- Modify: `docs/superpowers/specs/2026-09-17-mattermost-integration-design.md` (section "`sovrag.auth.mattermost`")

**Why.** Verified in the Mattermost server source at v11.11.0: `GET /api/v4/users/{id}/custom_profile_attributes` builds its reply from whatever values exist for the ID and never checks the user exists, and the bot's access check passes for any ID when the bot has no view restrictions. An ID Mattermost does not know therefore returns `200 {}`, identical to a known user with no attributes, and rule 4 (unknown user is an error) cannot be implemented with the two calls the spec names. `GET /api/v4/users/{id}` returns 404 for an unknown ID, and also carries `username` and `delete_at`.

- [ ] **Step 1: Add the amendment to ADR 0010**

Append to `docs/adr/0010-mattermost-attributes-as-entitlement-source.md`, after the Consequences list:

```markdown

## Amendment (2026-09-22): three calls, and the user call comes first

The values endpoint does not check that the user exists. In the server source at 11.11.0, `listCPAValues` returns whatever values are stored for the ID, and for a bot with no view restrictions the access check passes for any ID, so an ID Mattermost does not know answers `200` with an empty map. With the two calls above, an unknown user would be indistinguishable from a known user with no attributes and would be bound to least privilege, which rule 4 forbids.

The resolver therefore makes three calls, in this order, all with the bot token:

1. `GET /api/v4/users/{id}`. A 404 means Mattermost does not know the user; a user with `delete_at` set is deactivated. Both are resolution failures. The reply's `username` becomes the principal's id, so the audit log names the person the way Mattermost does. A user whose username is the reserved id `anonymous` cannot be bound to an entitled principal (ADR 0012) and is a resolution failure too.
2. `GET /api/v4/custom_profile_attributes/fields`.
3. `GET /api/v4/users/{id}/custom_profile_attributes`.

The rules are otherwise unchanged. Any transport error, timeout, non-200 status, or reply that is not the expected JSON shape on any of the three calls is a resolution failure. Three calls per question is still cheap beside an embedding call and a chat completion.
```

- [ ] **Step 2: Update the spec's resolver section**

In the spec, replace the sentences

```
It calls two REST endpoints with the bot token:
`GET /api/v4/custom_profile_attributes/fields` for the field definitions and
`GET /api/v4/users/{id}/custom_profile_attributes` for the values. The caller has already
checked that `user_id` is shaped like a Mattermost ID, so it is safe in the URL path.
```

with

```
It calls three REST endpoints with the bot token, in order: `GET /api/v4/users/{id}` to
prove the user exists and read the username, `GET /api/v4/custom_profile_attributes/fields`
for the field definitions, and `GET /api/v4/users/{id}/custom_profile_attributes` for the
values. The values endpoint answers an unknown ID with an empty map, which is why the user
call comes first (ADR 0010, amendment). The caller has already checked that `user_id` is
shaped like a Mattermost ID; the resolver checks again because the ID goes into a URL path.
```

and change rule 4 from `If either call fails, times out, or reports an unknown user` to `If any call fails, times out, or the user is unknown or deactivated`, and the closing paragraph from `Two REST calls per resolution` to `Three REST calls per resolution`.

- [ ] **Step 3: Format and commit**

```bash
uv run ruff format docs
uv run ruff format --check .
git add docs/adr/0010-mattermost-attributes-as-entitlement-source.md docs/superpowers/specs/2026-09-17-mattermost-integration-design.md docs/superpowers/plans/2026-09-22-mattermost-resolver.md
git commit -m "Amend ADR 0010: the resolver must confirm the user exists before reading attributes"
```

---

### Task 2: The audit log never tears a line under concurrent writers

**Files:**
- Modify: `src/sovrag/audit.py`
- Test: `tests/test_audit.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `write_event(event, **fields)` unchanged in signature; each call holds an exclusive `fcntl.flock` on the file for the duration of its single write.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_audit.py`:

```python
def test_concurrent_writers_never_tear_or_interleave_a_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight threads each append 50 records larger than the file buffer, so a write without a
    lock is split into several system calls that can interleave with another thread's."""
    import threading

    target = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_PATH", str(target))
    payload = "x" * 70_000

    def writer(n: int) -> None:
        for i in range(50):
            write_event("search", writer=n, i=i, blob=payload)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 400
    records = [json.loads(line) for line in lines]
    assert sorted((r["writer"], r["i"]) for r in records) == sorted(
        (n, i) for n in range(8) for i in range(50)
    )
    assert all(r["blob"] == payload for r in records)
```

- [ ] **Step 2: Run the test to see whether it fails**

Run: `uv run pytest tests/test_audit.py -q -k concurrent`

Expected: a `json.JSONDecodeError` or a wrong line count, from two threads' buffered writes interleaving. If it passes, the platform happened to serialise the appends; record that in the commit message and keep the test as the regression guard. The lock is still required by the spec for the MCP server's concurrent handlers.

- [ ] **Step 3: Take the lock**

Replace `src/sovrag/audit.py` with:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_audit.py -q && uv run mypy src && uv run ruff check .`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/sovrag/audit.py tests/test_audit.py
git commit -m "Lock the audit file around each append so concurrent writers never tear a line"
```

---

### Task 3: Settings for mattermost mode

**Files:**
- Modify: `src/sovrag/config.py`
- Modify: `.env.example`

**Interfaces:**
- Produces: `Settings.mattermost_url: str = ""` (env `MATTERMOST_URL`) and `Settings.mattermost_bot_token: str = ""` (env `MATTERMOST_BOT_TOKEN`).

- [ ] **Step 1: Add the settings**

In `src/sovrag/config.py`, after the `principal: str = ""` line, add:

```python
    # MCP server, mattermost mode only: where the resolver reads user attributes, and the
    # bot token it reads them with. The token is a credential; it is never logged.
    mattermost_url: str = ""
    mattermost_bot_token: str = ""
```

In `.env.example`, after the `PRINCIPAL=analyst_dnd` line, add:

```
# MCP server, mattermost mode only (not implemented yet). The resolver reads user attributes
# from MATTERMOST_URL with MATTERMOST_BOT_TOKEN. See ADR 0010.
MATTERMOST_URL=http://localhost:8065
MATTERMOST_BOT_TOKEN=
```

No test: the settings are exercised by `MattermostResolver.from_settings` in Task 4.

- [ ] **Step 2: Commit**

```bash
uv run ruff check . && uv run mypy src
git add src/sovrag/config.py .env.example
git commit -m "Add the Mattermost URL and bot token settings for the resolver"
```

---

### Task 4: The resolver

**Files:**
- Create: `src/sovrag/auth/mattermost.py`
- Modify: `src/sovrag/auth/__init__.py`
- Test: `tests/test_mattermost_resolver.py`

**Interfaces:**
- Consumes: `LEAST_PRIVILEGED`, `MEMBERSHIPS`, `Clearance`, `Principal` from `sovrag.auth.principal`; `Settings` from `sovrag.config`.
- Produces, importable from `sovrag.auth`:
  - `class ResolutionError(Exception)`
  - `@dataclass(frozen=True) class Resolution` with `user_id: str`, `username: str`, `principal: Principal`, `attributes: Mapping[str, str | list[str]]`, `note: str | None`
  - `class MattermostResolver` with `__init__(self, base_url: str, bot_token: str, *, timeout_s: float = 5.0, transport: httpx2.BaseTransport | None = None)`, `classmethod from_settings(settings: Settings) -> MattermostResolver` (raises `ValueError` when either setting is empty), and `resolve(self, user_id: str) -> Resolution`.
  - Constants `CLEARANCE_FIELD = "sovrag_clearance"`, `DEPARTMENTS_FIELD = "sovrag_departments"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mattermost_resolver.py`:

```python
"""Mattermost attribute resolver: recorded responses in, Principal out, errors on doubt."""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from sovrag.auth import (
    LEAST_PRIVILEGED,
    Clearance,
    MattermostResolver,
    Resolution,
    ResolutionError,
)
from sovrag.auth.mattermost import CLEARANCE_FIELD, DEPARTMENTS_FIELD
from sovrag.config import Settings

# Mattermost IDs are 26 lowercase base-32 characters. These are shaped like real ones.
UID_DND = "u1dnd0000000000000000000000"[:26]
UID_AUD = "u2aud0000000000000000000000"[:26]
UID_PUB = "u3pub0000000000000000000000"[:26]
UID_GONE = "u4gone000000000000000000000"[:26]
F_CLR = "f1clr0000000000000000000000"[:26]
F_DEP = "f2dep0000000000000000000000"[:26]
F_TXT = "f3txt0000000000000000000000"[:26]
OPT = {
    name: f"o{name.lower()}".ljust(26, "0")
    for name in (
        "UNCLASSIFIED",
        "PROTECTED_A",
        "PROTECTED_B",
        "DND",
        "CSE",
        "PS",
        "PSPC",
        "SSC",
        "TBS",
    )
}


def _field(fid: str, name: str, ftype: str, options: list[str], managed: str = "admin") -> dict:
    return {
        "id": fid,
        "group_id": "g1",
        "name": name,
        "type": ftype,
        "attrs": {
            "visibility": "when_set",
            "sort_order": 0,
            "options": [{"id": OPT[o], "name": o, "color": ""} for o in options],
            "value_type": "",
            "ldap": "",
            "saml": "",
            "managed": managed,
        },
        "target_id": "",
        "target_type": "",
        "object_type": "user",
        "create_at": 1,
        "update_at": 1,
        "delete_at": 0,
    }


FIELDS = [
    _field(F_CLR, CLEARANCE_FIELD, "select", ["UNCLASSIFIED", "PROTECTED_A", "PROTECTED_B"]),
    _field(F_DEP, DEPARTMENTS_FIELD, "multiselect", ["DND", "CSE", "PS", "PSPC", "SSC", "TBS"]),
    {**_field(F_TXT, "title", "text", []), "attrs": {"managed": "", "options": []}},
]

USERS = {
    UID_DND: {"id": UID_DND, "username": "analyst_dnd", "delete_at": 0},
    UID_AUD: {"id": UID_AUD, "username": "auditor", "delete_at": 0},
    UID_PUB: {"id": UID_PUB, "username": "public", "delete_at": 0},
}

VALUES = {
    UID_DND: {F_CLR: OPT["PROTECTED_A"], F_DEP: [OPT["DND"]], F_TXT: "Analyst"},
    UID_AUD: {
        F_CLR: OPT["PROTECTED_B"],
        F_DEP: [OPT[d] for d in ("CSE", "DND", "PS", "PSPC", "SSC", "TBS")],
    },
    UID_PUB: {},
}


class FakeMattermost:
    """Routes the three GET paths; records every request so tests can assert the calls."""

    def __init__(
        self,
        *,
        fields: list[dict] | None = None,
        values: dict[str, dict] | None = None,
        users: dict[str, dict] | None = None,
        fail: dict[str, int | Exception] | None = None,
    ) -> None:
        self.fields = FIELDS if fields is None else fields
        self.values = VALUES if values is None else values
        self.users = USERS if users is None else users
        self.fail = fail or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for key, outcome in self.fail.items():
            if key in path:
                if isinstance(outcome, Exception):
                    raise outcome
                return httpx.Response(outcome, json={"message": "nope"})
        if path == "/api/v4/custom_profile_attributes/fields":
            return httpx.Response(200, json=self.fields)
        if path.startswith("/api/v4/users/") and path.endswith("/custom_profile_attributes"):
            uid = path.split("/")[4]
            return httpx.Response(200, json=self.values.get(uid, {}))
        if path.startswith("/api/v4/users/"):
            uid = path.split("/")[4]
            if uid in self.users:
                return httpx.Response(200, json=self.users[uid])
            return httpx.Response(404, json={"id": "api.user.get.missing.app_error"})
        return httpx.Response(500, json={"message": f"unexpected {path}"})


def _resolver(server: FakeMattermost) -> MattermostResolver:
    return MattermostResolver("http://mm.test", "bot-token", transport=httpx.MockTransport(server))


def test_an_entitled_user_becomes_a_principal_named_by_username() -> None:
    server = FakeMattermost()
    result = _resolver(server).resolve(UID_DND)
    assert isinstance(result, Resolution)
    assert result.principal.id == "analyst_dnd"
    assert result.principal.clearance is Clearance.PROTECTED_A
    assert result.principal.departments == frozenset({"DND"})
    assert result.attributes == {CLEARANCE_FIELD: "PROTECTED_A", DEPARTMENTS_FIELD: ["DND"]}
    assert result.note is None
    assert result.user_id == UID_DND and result.username == "analyst_dnd"


def test_the_auditor_holds_every_membership() -> None:
    result = _resolver(FakeMattermost()).resolve(UID_AUD)
    assert result.principal.clearance is Clearance.PROTECTED_B
    assert result.principal.departments == frozenset({"CSE", "DND", "PS", "PSPC", "SSC", "TBS"})
    assert result.attributes[DEPARTMENTS_FIELD] == ["CSE", "DND", "PS", "PSPC", "SSC", "TBS"]


def test_three_calls_in_order_with_the_bot_token_and_nothing_else() -> None:
    server = FakeMattermost()
    _resolver(server).resolve(UID_DND)
    assert [r.url.path for r in server.requests] == [
        f"/api/v4/users/{UID_DND}",
        "/api/v4/custom_profile_attributes/fields",
        f"/api/v4/users/{UID_DND}/custom_profile_attributes",
    ]
    assert all(r.method == "GET" for r in server.requests)
    assert all(r.headers["authorization"] == "Bearer bot-token" for r in server.requests)


def test_a_known_user_with_no_values_is_least_privileged_with_a_note() -> None:
    result = _resolver(FakeMattermost()).resolve(UID_PUB)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {}
    assert result.note is not None and CLEARANCE_FIELD in result.note


def test_a_value_on_a_field_that_is_not_admin_managed_is_ignored() -> None:
    fields = [
        _field(
            F_CLR,
            CLEARANCE_FIELD,
            "select",
            ["UNCLASSIFIED", "PROTECTED_A", "PROTECTED_B"],
            managed="",
        ),
        FIELDS[1],
    ]
    result = _resolver(FakeMattermost(fields=fields)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert CLEARANCE_FIELD not in result.attributes
    assert result.note is not None and "not admin-managed" in result.note


def test_a_clearance_with_no_departments_is_still_a_principal() -> None:
    values = {UID_DND: {F_CLR: OPT["PROTECTED_A"]}}
    result = _resolver(FakeMattermost(values=values)).resolve(UID_DND)
    assert result.principal.clearance is Clearance.PROTECTED_A
    assert result.principal.departments == frozenset()
    assert result.attributes == {CLEARANCE_FIELD: "PROTECTED_A"}


def test_an_option_id_the_definition_does_not_list_is_ignored() -> None:
    values = {UID_DND: {F_CLR: "zzzzzzzzzzzzzzzzzzzzzzzzzz", F_DEP: [OPT["DND"], "nope"]}}
    result = _resolver(FakeMattermost(values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {DEPARTMENTS_FIELD: ["DND"]}
    assert result.note is not None and "unrecognised" in result.note


def test_an_option_name_outside_the_vocabulary_is_ignored() -> None:
    extra = "ozzz".ljust(26, "0")
    fields = [
        {
            **FIELDS[0],
            "attrs": {
                **FIELDS[0]["attrs"],
                "options": [*FIELDS[0]["attrs"]["options"], {"id": extra, "name": "SECRET"}],
            },
        },
        {
            **FIELDS[1],
            "attrs": {
                **FIELDS[1]["attrs"],
                "options": [*FIELDS[1]["attrs"]["options"], {"id": extra, "name": "GC_WIDE"}],
            },
        },
    ]
    values = {UID_DND: {F_CLR: extra, F_DEP: [extra, OPT["DND"]]}}
    result = _resolver(FakeMattermost(fields=fields, values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {DEPARTMENTS_FIELD: ["DND"]}


def test_a_select_field_of_the_wrong_type_is_ignored() -> None:
    fields = [_field(F_CLR, CLEARANCE_FIELD, "text", []), FIELDS[1]]
    values = {UID_DND: {F_CLR: "PROTECTED_B", F_DEP: [OPT["DND"]]}}
    result = _resolver(FakeMattermost(fields=fields, values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED


def test_a_value_of_the_wrong_json_shape_is_ignored() -> None:
    values = {UID_DND: {F_CLR: [OPT["PROTECTED_A"]], F_DEP: OPT["DND"]}}
    result = _resolver(FakeMattermost(values=values)).resolve(UID_DND)
    assert result.principal == LEAST_PRIVILEGED
    assert result.attributes == {}


def test_an_unknown_user_is_an_error_not_a_downgrade() -> None:
    server = FakeMattermost()
    with pytest.raises(ResolutionError, match="unknown user"):
        _resolver(server).resolve(UID_GONE)
    assert len(server.requests) == 1


def test_a_deactivated_user_is_an_error() -> None:
    users = {**USERS, UID_DND: {**USERS[UID_DND], "delete_at": 1700000000000}}
    with pytest.raises(ResolutionError, match="deactivated"):
        _resolver(FakeMattermost(users=users)).resolve(UID_DND)


def test_a_user_named_anonymous_cannot_be_entitled() -> None:
    users = {**USERS, UID_DND: {**USERS[UID_DND], "username": "anonymous"}}
    with pytest.raises(ResolutionError, match="anonymous"):
        _resolver(FakeMattermost(users=users)).resolve(UID_DND)


@pytest.mark.parametrize(
    "path_part",
    ["/api/v4/users/", "custom_profile_attributes/fields", "/custom_profile_attributes"],
)
@pytest.mark.parametrize(
    "outcome",
    [500, 401, httpx.ConnectTimeout("slow"), httpx.ConnectError("down")],
    ids=["500", "401", "timeout", "refused"],
)
def test_any_failed_call_is_an_error_and_never_least_privilege(
    path_part: str, outcome: int | Exception
) -> None:
    key = "/api/v4/users/" if path_part == "/api/v4/users/" else path_part
    server = FakeMattermost(fail={key: outcome})
    with pytest.raises(ResolutionError):
        _resolver(server).resolve(UID_DND)


def test_a_reply_that_is_not_json_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>login</html>")

    resolver = MattermostResolver("http://mm.test", "t", transport=httpx.MockTransport(handler))
    with pytest.raises(ResolutionError, match="not JSON"):
        resolver.resolve(UID_DND)


def test_a_fields_reply_of_the_wrong_shape_is_an_error() -> None:
    server = FakeMattermost(fields={"not": "a list"})  # type: ignore[arg-type]
    with pytest.raises(ResolutionError, match="shape"):
        _resolver(server).resolve(UID_DND)


@pytest.mark.parametrize("bad", ["", "short", "../admin", "A" * 26, "x" * 27, "u1dnd/../"])
def test_a_malformed_user_id_never_reaches_the_network(bad: str) -> None:
    server = FakeMattermost()
    with pytest.raises(ResolutionError, match="malformed"):
        _resolver(server).resolve(bad)
    assert server.requests == []


def test_the_token_never_appears_in_an_error_or_resolution() -> None:
    server = FakeMattermost(fail={"fields": 500})
    resolver = MattermostResolver(
        "http://mm.test", "s3cret-token", transport=httpx.MockTransport(server)
    )
    with pytest.raises(ResolutionError) as info:
        resolver.resolve(UID_DND)
    assert "s3cret" not in str(info.value) and "s3cret" not in repr(info.value)
    ok = _resolver(FakeMattermost()).resolve(UID_DND)
    assert "bot-token" not in repr(ok)


def test_from_settings_requires_both_values() -> None:
    with pytest.raises(ValueError, match="MATTERMOST_URL"):
        MattermostResolver.from_settings(Settings(mattermost_url="", mattermost_bot_token="t"))
    with pytest.raises(ValueError, match="MATTERMOST_BOT_TOKEN"):
        MattermostResolver.from_settings(
            Settings(mattermost_url="http://mm.test", mattermost_bot_token="")
        )
    resolver = MattermostResolver.from_settings(
        Settings(mattermost_url="http://mm.test/", mattermost_bot_token="t")
    )
    assert isinstance(resolver, MattermostResolver)


def test_resolution_is_json_serialisable_for_the_audit_log() -> None:
    result = _resolver(FakeMattermost()).resolve(UID_AUD)
    line = json.dumps(
        {"principal": result.principal.id, "attributes": result.attributes, "note": result.note}
    )
    assert json.loads(line)["attributes"][CLEARANCE_FIELD] == "PROTECTED_B"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_mattermost_resolver.py -q`
Expected: `ImportError: cannot import name 'MattermostResolver' from 'sovrag.auth'`.

- [ ] **Step 3: Implement the resolver**

Create `src/sovrag/auth/mattermost.py`:

```python
"""Resolve a Mattermost user to a Principal from admin-managed user attributes (ADR 0010).

Three GET calls with the bot token, in order: the user (proves the ID exists, gives the
username), the attribute field definitions, and the user's values. Only admin-managed
select fields named sovrag_clearance and sovrag_departments count, and their option IDs are
mapped to names through the definitions fetched in the same resolution. A known user with
nothing recognised is LEAST_PRIVILEGED with a note saying why. Anything the resolver cannot
verify, a transport error, a non-200 status, an unknown or deactivated user, a reply of the
wrong shape, is a ResolutionError, never a downgrade (ADR 0009).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx2

from sovrag.auth.principal import ANONYMOUS_ID, LEAST_PRIVILEGED, MEMBERSHIPS, Clearance, Principal
from sovrag.config import Settings

CLEARANCE_FIELD = "sovrag_clearance"
DEPARTMENTS_FIELD = "sovrag_departments"
ADMIN_MANAGED = "admin"
_USER_ID = re.compile(r"^[a-z0-9]{26}$")


class ResolutionError(Exception):
    """Mattermost could not tell us what the user is entitled to."""


@dataclass(frozen=True)
class Resolution:
    user_id: str
    username: str
    principal: Principal
    attributes: Mapping[str, str | list[str]]
    note: str | None


class MattermostResolver:
    def __init__(
        self,
        base_url: str,
        bot_token: str,
        *,
        timeout_s: float = 5.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=httpx2.Timeout(timeout_s, connect=5.0),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> MattermostResolver:
        if not settings.mattermost_url:
            raise ValueError("MATTERMOST_URL is required in mattermost mode")
        if not settings.mattermost_bot_token:
            raise ValueError("MATTERMOST_BOT_TOKEN is required in mattermost mode")
        return cls(settings.mattermost_url, settings.mattermost_bot_token)

    def resolve(self, user_id: str) -> Resolution:
        if not _USER_ID.match(user_id):
            raise ResolutionError("malformed user id")
        user = self._get(f"/api/v4/users/{user_id}", unknown_on_404=True)
        if not isinstance(user, dict) or not isinstance(user.get("username"), str):
            raise ResolutionError("user reply has the wrong shape")
        if user.get("delete_at"):
            raise ResolutionError(f"user {user_id} is deactivated")
        username: str = user["username"]
        fields = self._get("/api/v4/custom_profile_attributes/fields")
        if not isinstance(fields, list):
            raise ResolutionError("fields reply has the wrong shape")
        values = self._get(f"/api/v4/users/{user_id}/custom_profile_attributes")
        if not isinstance(values, dict):
            raise ResolutionError("values reply has the wrong shape")
        return _bind(user_id, username, fields, values)

    def _get(self, path: str, *, unknown_on_404: bool = False) -> Any:
        try:
            response = self._client.get(path)
        except httpx2.HTTPError as exc:
            raise ResolutionError(f"{path}: {type(exc).__name__}") from None
        if response.status_code == 404 and unknown_on_404:
            raise ResolutionError("unknown user")
        if response.status_code != 200:
            raise ResolutionError(f"{path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise ResolutionError(f"{path}: reply is not JSON") from None


def _bind(user_id: str, username: str, fields: list[Any], values: Mapping[str, Any]) -> Resolution:
    notes: list[str] = []
    snapshot: dict[str, str | list[str]] = {}

    clearance: Clearance | None = None
    field = _admin_managed(fields, CLEARANCE_FIELD, "select", notes)
    if field is not None:
        raw = values.get(field["id"])
        if raw is None:
            notes.append(f"no {CLEARANCE_FIELD} value")
        elif not isinstance(raw, str):
            notes.append(f"{CLEARANCE_FIELD} value has the wrong shape")
        else:
            name = _option_name(field, raw)
            if name is None:
                notes.append(f"unrecognised {CLEARANCE_FIELD} option")
            elif name not in Clearance.__members__:
                notes.append(f"{CLEARANCE_FIELD} {name!r} is outside the vocabulary")
            else:
                clearance = Clearance(name)
                snapshot[CLEARANCE_FIELD] = name

    departments: set[str] = set()
    field = _admin_managed(fields, DEPARTMENTS_FIELD, "multiselect", notes)
    if field is not None:
        raw = values.get(field["id"])
        if raw is None:
            notes.append(f"no {DEPARTMENTS_FIELD} value")
        elif not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            notes.append(f"{DEPARTMENTS_FIELD} value has the wrong shape")
        else:
            for option_id in raw:
                name = _option_name(field, option_id)
                if name is None:
                    notes.append(f"unrecognised {DEPARTMENTS_FIELD} option")
                elif name not in MEMBERSHIPS:
                    notes.append(f"{DEPARTMENTS_FIELD} {name!r} is outside the vocabulary")
                else:
                    departments.add(name)
            if departments:
                snapshot[DEPARTMENTS_FIELD] = sorted(departments)

    if clearance is None:
        notes.append("bound to least privilege")
        return Resolution(user_id, username, LEAST_PRIVILEGED, snapshot, "; ".join(notes))
    if username.lower() == ANONYMOUS_ID:
        raise ResolutionError(f"username {username!r} is reserved and cannot be entitled")
    principal = Principal(id=username, clearance=clearance, departments=frozenset(departments))
    return Resolution(user_id, username, principal, snapshot, "; ".join(notes) or None)


def _admin_managed(
    fields: list[Any], name: str, expected_type: str, notes: list[str]
) -> dict[str, Any] | None:
    for field in fields:
        if not isinstance(field, dict) or field.get("name") != name:
            continue
        attrs = field.get("attrs")
        if not isinstance(attrs, dict) or attrs.get("managed") != ADMIN_MANAGED:
            notes.append(f"{name} is not admin-managed")
            return None
        if field.get("type") != expected_type or not isinstance(field.get("id"), str):
            notes.append(f"{name} is not a {expected_type} field")
            return None
        return field
    notes.append(f"no {name} field")
    return None


def _option_name(field: Mapping[str, Any], option_id: str) -> str | None:
    options = field.get("attrs", {}).get("options")
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and option.get("id") == option_id:
            name = option.get("name")
            return name if isinstance(name, str) else None
    return None
```

In `src/sovrag/auth/__init__.py`, add the import and the three names to `__all__`:

```python
from sovrag.auth.mattermost import MattermostResolver, Resolution, ResolutionError
```

Keep `__all__` sorted. `sovrag.auth.mattermost` imports `sovrag.config`, and `sovrag.config` imports nothing from `sovrag`, so there is no cycle.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_mattermost_resolver.py -q && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: all pass. Two tests deserve a second look if they fail:
- `test_a_value_on_a_field_that_is_not_admin_managed_is_ignored` expects the note to contain "not admin-managed"; `_admin_managed` produces it.
- `test_a_known_user_with_no_values_is_least_privileged_with_a_note` expects the note to mention `sovrag_clearance`; the "no sovrag_clearance value" note does.

- [ ] **Step 5: Commit**

```bash
git add src/sovrag/auth/mattermost.py src/sovrag/auth/__init__.py tests/test_mattermost_resolver.py
git commit -m "Resolve a Mattermost user to a principal from admin-managed attributes"
```

---

### Task 5: Documentation

**Files:**
- Modify: `docs/architecture.md` (Principals section)

- [ ] **Step 1: Describe the resolver**

In `docs/architecture.md`, in "## Principals", after the paragraph that begins "Like the classification labels, clearances are synthetic", add:

```markdown
For Mattermost users, `auth/mattermost.py` resolves a user ID to a principal with three REST calls made with the bot token: the user, which proves the ID exists and supplies the username that becomes the principal's id; the attribute field definitions; and the user's values. Only admin-managed `sovrag_clearance` and `sovrag_departments` fields count, option IDs are mapped to names through the definitions fetched in the same call, and a name outside the vocabulary is ignored. A known user with nothing recognised is bound to `LEAST_PRIVILEGED` with a note saying why. An unknown or deactivated user, a failed call, a timeout, or a reply of the wrong shape is a `ResolutionError`; a failed lookup says nothing about entitlement and is never least privilege. The audit log takes an exclusive lock around each append so the MCP server's concurrent handlers never tear a line. See ADR 0010.
```

- [ ] **Step 2: Full check and commit**

```bash
uv run ruff format docs
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q
git add docs/architecture.md
git commit -m "Describe the Mattermost resolver and the audit lock"
```

---

## Pull request

Push `mattermost-resolver` and open a pull request against `main` titled "Resolve Mattermost users to principals from admin-managed attributes". The body covers the ADR 0010 amendment and why (the values endpoint's behaviour for an unknown ID), the three calls, the rules, the audit lock, and the settings. Wait for `ci` and `claude-review` on the head commit; the review must post a "## Code review" comment. Squash-merge with `gh pr merge --squash --delete-branch`.

## Self-review

- Spec coverage: `Resolution` with principal, attribute snapshot and note (Task 4); `ResolutionError` (Task 4); rule 1 admin-managed only, definitions fetched every time (Task 4, `_admin_managed`); rule 2 option IDs to names, vocabulary check (Task 4, `_option_name`); rule 3 least privilege with note (Task 4, `_bind`); rule 4 failures are errors (Task 4, `_get`, and the unknown-user fix in Task 1); no cache (nothing cached); audit lock (Task 2); settings `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN` (Task 3); tests listed in the spec for the resolver and for concurrent audit writers (Tasks 2 and 4).
- Names used across tasks: `MattermostResolver`, `Resolution`, `ResolutionError`, `CLEARANCE_FIELD`, `DEPARTMENTS_FIELD`, `from_settings`, `mattermost_url`, `mattermost_bot_token` are spelled the same everywhere.
