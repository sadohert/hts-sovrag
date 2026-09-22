# Mattermost demo: the same question, three answers

Three demo users ask the sovrag bot the same question in a direct message and get three
different answers, because Mattermost tells sovrag who is asking and sovrag's database only
returns what that person may see. Nothing leaves the machine: Mattermost, the Agents plugin,
the model, the embedder, the MCP server and Postgres all run locally.

Everything here is synthetic. The corpus is public Government of Canada documents; the
classification labels, clearances, departments and users are invented so the entitlement model
has something to enforce (ADR 0003). No protected information was used.

## Prerequisites

- Docker Desktop with Model Runner enabled, and the two models pulled (`docker compose up`
  does that on first use). Mattermost publishes amd64 images only, so on Apple silicon it runs
  under emulation; allow a minute for the first boot.
- The corpus indexed: `uv run sovrag ingest` once.
- Mattermost Entry needs no licence key. It caps message history at 10,000 and is meant for
  small teams; see ADR 0011.

## Setup

```
docker compose --profile mattermost up -d            # postgres, the db-init step, Mattermost
uv run python scripts/seed_mattermost.py             # users, attributes, plugin, bot, token
```

The seed script prints four lines for `.env`: `MATTERMOST_URL`, `MATTERMOST_BOT_TOKEN`,
`MCP_SHARED_SECRET` and `MCP_IDENTITY=mattermost`. Put them in `.env`, then:

```
docker compose --profile mattermost --profile mcp up -d mcp
docker compose --profile mcp exec mcp tail -f /app/audit/audit.jsonl   # keep this open
```

Open http://localhost:8065. Sign in as `admin` (password `Passw0rd!admin` unless you changed it)
once to create a team and add the three users to it; that is the one manual step, because a
team is a workspace choice, not configuration. Then sign in as each demo user in a private
window and open a direct message with `@sovrag`.

## The users

| user | clearance | departments | sees | never sees |
|---|---|---|---|---|
| `analyst_dnd` | PROTECTED_A | DND | departmental plans, policies, the DND results report | other departments' results reports, Auditor General reports |
| `auditor` | PROTECTED_B | all six | everything | nothing |
| `public` | UNCLASSIFIED | none | departmental plans, policies | every results report, every Auditor General report |

Password for all three: `Passw0rd!demo`. The attributes are admin-managed: a user cannot change
their own (ADR 0010).

## The beats

1. **`analyst_dnd` asks:** "What did National Defence report as its results for 2024-25?"
   The bot answers with citations into the DND Departmental Results Report.
2. **`public` asks the same.** The bot refuses: the results reports are PROTECTED_A and `public`
   is unclassified. The refusal says nothing about what exists; the audit tail shows
   `retrieved_chunk_ids: []` and the filter that was applied.
3. **`auditor` asks:** "What did the Auditor General find about ArriveCAN?" An answer with
   citations into the Auditor General's report.
4. **`analyst_dnd` asks the same.** Refused: the Auditor General reports are PROTECTED_B.
5. **The admin raises `analyst_dnd` to PROTECTED_B** in System Console, User Management, Users,
   `analyst_dnd`, User Attributes. `analyst_dnd` asks again and gets the answer. No restart,
   no cache: the resolver reads the attributes on every question.

What each beat proves: 1 and 3, retrieval and grounded answers work through Mattermost; 2 and
4, the same question gives a different answer to a different person, decided in SQL from the
principal the server bound, not by the model; 5, entitlement is managed where users are managed
and takes effect on the next question.

## What to look at in the audit tail

Each question writes a `resolve` line (the Mattermost user id, the attribute snapshot, the
principal it became, or the reason it was least privilege) and an `answer` line (the principal,
the rendered filter, the chunk ids retrieved and cited, `refused`) that share a `request_id`.
A refusal and an outage look different: an outage is a tool error and writes no `answer` line.

## Checks

- The `mattermost` role cannot open the sovrag database:
  `docker compose exec postgres psql -U mattermost -d sovrag -c 'select 1'` fails with
  "permission denied for database sovrag".
- A request without the shared secret gets 401 before anything runs:
  `curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:8002/mcp -H 'Content-Type: application/json' -d '{}'`
  prints `401`.
- The plugin's MCP server entry has an empty static header map and only `Authorization` in the
  service-account map; `tests/test_seed_mattermost.py` asserts it, because a header named
  `X-Mattermost-UserID` in either map would override every user's identity (ADR 0009).

## Reset

`docker compose --profile mattermost --profile mcp down -v` removes Mattermost's data and the
audit volume. The sovrag index is in the `postgres` volume; `down -v` removes that too, so
re-run `uv run sovrag ingest` afterwards.
