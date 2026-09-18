# ADR 0011: Mattermost runs in Compose as Mattermost Entry, on the existing Postgres

Status: accepted (2026-09-17)

## Context

The Mattermost demo (ADR 0009, ADR 0010) needs a Mattermost server with user attributes, the Agents plugin with a remote MCP server, and a local model. Compose is the whole deployment story for sovrag, so Mattermost has to fit into `compose.yaml` without a licence purchase and without a second way of running things.

Checked on 2026-09-17:

| Item | Finding | How it was checked |
|---|---|---|
| Newest server release | 11.11.0, released 2026-09-07 | GitHub releases for `mattermost/mattermost` |
| Image | `mattermost/mattermost-enterprise-edition:11.11.0`, digest `sha256:f0643c47b55bd7be78643ee79b681e0ec5e11da32f64c7dfba367399d157454e` | Docker Hub, then pulled |
| Architectures | `linux/amd64` only, for the enterprise and the team edition images | Docker Hub tag metadata; a pull on Apple silicon fails without a platform override |
| Licence with no key installed | The server logs "Mattermost Entry is enabled. Unlocking enterprise features." and reports `SkuShortName: entry` | booted the image and read the log and `/api/v4/license/client` |
| Postgres support | Documentation states a minimum, "PostgreSQL 14.0+", and no maximum | Mattermost software requirements page |
| Postgres 18 | 11.11.0 booted against PostgreSQL 18.6 from the project's pinned `pgvector/pgvector:0.8.6-pg18-trixie` image; migrations created 115 tables; `/api/v4/system/ping` reported `database_status: OK` | booted both containers on a throwaway network |
| Agents plugin bundled in the image | v2.6.1 | listed `prepackaged_plugins` in the image |
| Newest Agents plugin | v2.7.0, released 2026-09-02, minimum server 11.9.0 | GitHub releases for `mattermost/mattermost-plugin-agents` |

## Decision

**Edition.** A `mattermost` Compose profile runs the enterprise image with no licence key, which is Mattermost Entry. Entry has the Enterprise Advanced feature set this design needs: user attributes, attribute-based access control, more than one bot, and remote MCP servers. The team edition image cannot become Entry.

**Platform.** The service sets `platform: linux/amd64`. On Apple silicon it runs under Docker Desktop's emulation, which is adequate for a demo; on an amd64 host it runs natively.

**Database.** Mattermost uses the existing `postgres` service with its own database and its own role, both named `mattermost`. Because Postgres init scripts only run on an empty volume, a one-shot service under the same profile creates the role and database if they are missing, so the profile works on an existing development volume.

Keeping the `mattermost` role out of the `sovrag` database takes an explicit step, because Postgres grants `CONNECT` and `TEMPORARY` on every database to `PUBLIC` by default. The one-shot service runs `REVOKE CONNECT, TEMPORARY ON DATABASE sovrag FROM PUBLIC`, and a check in the demo walkthrough shows the `mattermost` role being refused. The isolation runs one way only. The `sovrag` role is the container's `POSTGRES_USER`, a superuser, so the app can read the `mattermost` database, which holds sessions and the bot token. That is acceptable for a single-machine demo and is not a production layout, where the two would not share an instance or the app would not run as a superuser. One Postgres container keeps the "one Compose file" story honest; a second Postgres service remains the fallback if a future Mattermost release rejects Postgres 18.

**Agents plugin.** Pinned to v2.7.0, because the MCP service-account headers that carry the bearer token (ADR 0009) arrived in that release and the image bundles v2.6.1. The seed script uploads the v2.7.0 release asset, verified against a SHA-256 recorded in the script, and enables it.

**Model.** The plugin's provider is its "OpenAI Compatible" type pointed at Docker Model Runner on the Docker internal hostname, with "Use Responses API" off, since Model Runner serves chat completions. The plugin appends `/v1/chat/completions` to the base URL, so the base is Model Runner's `/engines` prefix; the exact value is confirmed when the profile is built. No request leaves the machine: Mattermost, the plugin, the generator, the embedder and Postgres are all local.

**Configuration is a script.** `scripts/seed_mattermost.py` creates users, attributes, the plugin configuration and the bot token through the REST API. Nothing is configured by hand in the System Console, so the demo is reproducible and reviewable.

## Not yet verified

- That the plugin's tool calling works through Model Runner with `ai/qwen3.5:9b-q4_K_M`. The plugin needs streaming chat completions with tool calls. If it does not work, the fallback is a different local model for the plugin's provider; sovrag's own generator is unaffected because the `answer` tool calls it directly.
- What happens when the Entry licence lapses. The boot log shows an Entry licence that starts at boot and expires 100 days later. Whether a restart renews it was not tested.
- The Entry limits in practice. Documentation lists 10,000 messages of viewable history, and a limit of 250 agent queries per month marked "will take effect in a future release".

## Consequences

- `compose.yaml` gains a `mattermost` profile with the Mattermost service and the one-shot database service. The default profile is unchanged.
- The image tag, its digest and the plugin version are pinned, and are re-checked whenever they are changed.
- The boot log contains one error line from the Agents plugin, "ffmpeg not installed, transcriptions will be disabled". It is expected and harmless here.
- Entry is "best suited for teams less than 50 users" with community support only. This is a demo and reference deployment, not a production recommendation, and the documentation says so.
- Only local Docker Desktop settings are used. No Docker organisation-level governance is applied or claimed.
