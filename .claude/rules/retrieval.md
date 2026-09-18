---
paths:
  - "src/sovrag/retrieve/**"
  - "src/sovrag/mcp/**"
  - "tests/test_*search*.py"
  - "tests/test_filters.py"
  - "tests/test_mcp*.py"
---
# Retrieval and MCP

- Filtering by principal is a SQL WHERE clause. A test must show that a principal without
  entitlement gets zero rows, not a shorter list.
- A Hit carries doc_id, chunk_id, heading_path, source_url and score. Do not add a code
  path that returns chunk text without them.
- MCP tools take the query and search options only. The server binds the principal: from
  Settings at start (static mode) or once per tool call from an authenticated transport it
  verifies (mattermost mode). It is never a tool argument, and there is no fallback from one
  mode to the other (ADR 0009).
- Three outcomes, never blurred (ADR 0009). A request that fails authentication is rejected
  before any retrieval and is never mapped to the least privileged principal. A lookup that
  fails, times out, or names a user Mattermost does not know makes the tool call fail with an
  error and retrieves nothing; it is never a downgrade. Only a known user with no recognised
  entitlement is bound to the least privileged principal, and the audit log records why.
