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
- MCP tools take the query and search options only. The principal comes from Settings at
  server start and is never a tool argument.
