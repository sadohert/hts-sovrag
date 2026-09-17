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

In `mattermost` mode, before any tool runs:

1. `Authorization: Bearer <token>` is compared with `MCP_SHARED_SECRET` in constant time. Missing or wrong: HTTP 401.
2. `X-Mattermost-UserID` is read. Missing: HTTP 401, because the plugin always sends it and its absence means a different caller.
3. The user ID is resolved to a `Principal` from that user's Mattermost attributes (ADR 0010).

Rejection and downgrade are kept apart:

- A request that fails authentication is rejected. It is never mapped to the least privileged principal, so `anonymous` cannot become an unlogged way in.
- An authenticated user with no attributes, unrecognised values, or a failed attribute lookup is bound to `LEAST_PRIVILEGED`, and the audit record states the reason.
- On the CLI, an unknown principal id is a usage error.
- The server refuses to start if `MCP_IDENTITY` is unset or invalid, or if `static` mode names an unknown principal.

Tools keep the shape the original rule required: a question and search options, no principal parameter, and descriptions that say so.

## Why the header is trustworthy, and its limits

The bearer token authenticates the caller as the plugin. The plugin sets the user ID header itself from the authenticated Mattermost session of the person who sent the message; the user cannot influence it, and the model never sees it. This is the trusted-gateway pattern: the MCP server trusts the gateway, and the gateway vouches for the user.

The limits are the limits of that pattern. Anyone who holds the shared secret can claim any user ID, so the secret is an administrator credential: it is configured in the plugin by a system administrator, kept in the environment of the MCP service, and never logged. A Mattermost system administrator, or another plugin installed on the same server, could also act as any user; both are already fully trusted in a Mattermost deployment. The port should be reachable only from the Mattermost server. Per-user OAuth 2.1, which the plugin also supports, would remove the shared secret at the cost of an authorization server; it is out of scope and can replace this mode later without changing the tools.

## Consequences

- `CLAUDE.md` and `.claude/rules/retrieval.md` carry the new invariant text. The generation contract uses the same sentence for chat bots.
- Every MCP request in `mattermost` mode writes a `resolve` audit event with the user ID, the attribute snapshot, the resulting principal and any error, joined to the `search` or `answer` event by a `request_id`.
- Replies are restricted to direct messages. A bot mentioned in a channel answers in a thread every member can read, which would broadcast one user's entitled answer. The bot is configured with `channelAccessLevel: None`, and channel or group replies are out of scope.
- The leak tests must hold on the MCP path as well as the CLI path, for each identity mode.
