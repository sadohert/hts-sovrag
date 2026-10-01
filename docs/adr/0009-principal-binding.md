# ADR 0009: The server binds the principal, from configuration or from a transport it verifies

Status: accepted (2026-09-17)

## Context

The original rule was that the MCP server binds one principal from configuration at startup and that tool arguments never carry identity. That fits a local agent with one user. It does not fit a chat platform, where the natural principal is whoever sent the message and a single server process answers many users.

The Mattermost Agents plugin (v2.7.0) was read at source level to see what identity it passes on. Its OpenAI-compatible provider sends messages, tools and token limits and nothing else: no `user` field, no custom headers, no per-request hook. The only identity that reaches a model is prose in the system prompt. Its remote MCP path is different. Every connection carries the asking user's Mattermost ID in an `X-Mattermost-UserID` header, set by the plugin on the server side, and an administrator can attach static service-account headers such as a bearer token.

The reason behind the original rule still holds: whoever names the principal chooses the entitlement. The rule's wording, not its reason, was too narrow.

## Decision

The invariant becomes: a principal is bound by the server, from configuration or from an authenticated transport the server verifies. Identity never comes from message text, tool arguments, or any value an agent or model can set.

The MCP server has two identity modes, chosen by a required setting, `MCP_IDENTITY`. There is no default and no fallback from one mode to the other.

| Mode | Principal comes from | When |
|---|---|---|
| `static` | `PRINCIPAL`, resolved from `data/principals.yaml` at startup | one local user, tests, any MCP client without a trusted gateway |
| `mattermost` | the `X-Mattermost-UserID` header, after the bearer token is verified | the Agents plugin calling on behalf of many users |

In `mattermost` mode every HTTP request is authenticated, and the principal is resolved once per tool call.

Every request, in middleware:

1. `Authorization: Bearer <token>` is compared with `MCP_SHARED_SECRET` in constant time. Missing or wrong: HTTP 401.
2. `X-Mattermost-UserID` is read. Missing, empty, or not shaped like a Mattermost ID (26 lowercase letters and digits): HTTP 401. The plugin always sends a real ID, so anything else is a different caller, and the value is never placed in a URL before this check.

Each tool call, in the tool handler:

3. The user ID is resolved to a `Principal` from that user's Mattermost attributes (ADR 0010), and the retrieval runs as that principal.

Resolution belongs to the tool call, not to the middleware, because one tool call over Streamable HTTP is several HTTP requests (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`). Resolving on each would multiply the Mattermost lookups and write several `resolve` audit events per question, only one of which could be joined to an answer.

Rejection, failure and downgrade are three different outcomes:

- **Rejected.** A request that fails authentication gets 401. It is never mapped to the least privileged principal, so `anonymous` cannot become an unlogged way in.
- **Failed.** If Mattermost does not know the user ID, or either attribute lookup errors or times out, the tool call returns an error and nothing is retrieved. An outage must not look like a refusal: a user entitled to a document would otherwise get "the context does not contain the answer", or a confident answer built from unclassified material alone, with nothing to tell them the entitlement check never happened. The audit record states the cause.
- **Downgraded.** Only a user Mattermost knows, whose lookups succeeded, and who has no attributes or no recognised values is bound to `LEAST_PRIVILEGED`. That is a true statement about the user, and the audit record states the reason.
- On the CLI, an unknown principal id is a usage error.
- The server refuses to start if `MCP_IDENTITY` is unset or invalid, or if `static` mode names an unknown principal.

Tools keep the shape the original rule required: a question and search options, no principal parameter, and descriptions that say so.

## Why the header is trustworthy, and its limits

The bearer token authenticates the caller as the plugin. The plugin sets the user ID header itself from the authenticated Mattermost session of the person who sent the message; the person chatting cannot influence it, and the model never sees it. This is the trusted-gateway pattern: the MCP server trusts the gateway, and the gateway vouches for the user.

An administrator can break it by accident. The plugin builds its outgoing headers in layers, and later layers win: the user ID header first, then the administrator's static headers, then the service-account headers. A header named `X-Mattermost-UserID` in either administrator-controlled layer, for example one copied from a curl test, would replace every user's ID with that one value, and every user would get that user's entitlement with clean-looking audit lines. The bearer token lives in the service-account layer, the one that wins. The seed script therefore leaves the static header map empty and writes exactly one service-account header, `Authorization`, and a test asserts both maps: nothing in the first, only `Authorization` in the second.

The other limits are the limits of the pattern. Anyone who holds the shared secret can claim any user ID, so the secret is an administrator credential: it is configured in the plugin by a system administrator, kept in the environment of the MCP service, and never logged. A Mattermost system administrator, or another plugin installed on the same server, could also act as any user; both are already fully trusted in a Mattermost deployment. The port should be reachable only from the Mattermost server. Per-user OAuth 2.1, which the plugin also supports, would remove the shared secret at the cost of an authorization server; it is out of scope and can replace this mode later without changing the tools.

## Consequences

- `CLAUDE.md`, `.claude/rules/retrieval.md` and the comment on `PRINCIPAL` in `.env.example` carry the new invariant. The `mcp` service in `compose.yaml` still has its earlier shape, a default `PRINCIPAL` and a `Dockerfile.mcp` that does not exist; it is marked as not implemented, and it changes with the MCP server.
- Every tool call in `mattermost` mode writes one `resolve` audit event with the user ID, the attribute snapshot, the resulting principal or the failure, joined to the `search` or `answer` event by a `request_id`.
- The audit log becomes shared by concurrent writers: several users at once, and the `app` and `mcp` services on one volume. `sovrag.audit` appends without a lock, and its docstring says so. A single-writer or locking change lands before the MCP server writes to that file, so that lines cannot interleave.
- Replies are restricted to direct messages. A bot mentioned in a channel answers in a thread every member can read, which would broadcast one user's entitled answer. The bot is configured with `channelAccessLevel: None`, and channel or group replies are out of scope.
- The leak tests must hold on the MCP path as well as the CLI path, for each identity mode.

## Amendment (2026-09-22): the bearer goes in the static header map, not the service-account map

The first live run of the demo resolved every question to the bot itself. In plugin 2.7.0, turning on service-account authentication for an agent makes the plugin own one connection pool for the remote MCP server as the bot user, and that pool's `X-Mattermost-UserID` is the bot's own ID; the person asking is not transmitted to a remote server at all. With service-account authentication off, the plugin opens per-user connections whose `X-Mattermost-UserID` is the asking user's, and layers the administrator's static header map on top.

So the shared secret lives in the static header map, service-account authentication is off, and the service-account map is empty. The risk described above is unchanged in kind: the static map still wins over the plugin's user-ID header, so a header named `X-Mattermost-UserID` placed there would replace every user's identity. The seed script writes exactly one static header, `Authorization`, leaves the service-account map empty, and a test asserts both. Verified on the live stack: after the change, each demo user's question resolved to that user's own attributes.
