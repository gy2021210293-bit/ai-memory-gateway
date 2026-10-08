# AI Memory Gateway project guidance

## Project overview

AI Memory Gateway is a Python FastAPI service that proxies OpenAI-compatible chat requests and adds durable memory. It stores conversations and memories in PostgreSQL, extracts memories in the background, retrieves relevant memories before forwarding a request, and serves a Jinja-based dashboard.

## Important files

- `main.py`: FastAPI app, OpenAI-compatible gateway routes, dashboard APIs.
- `database.py`: PostgreSQL schema and all persistence operations.
- `memory_extractor.py`: memory extraction and consolidation logic.
- `templates/dashboard.html`, `static/`: dashboard UI.
- `system_prompt.txt`: companion persona prompt.

## Working rules

- Preserve the gateway request path as the single source of truth for conversation ingestion, memory extraction, and retrieval.
- Use PostgreSQL as the canonical memory store; do not introduce a second independent memory database.
- Keep API keys in environment variables or existing protected runtime configuration, never in frontend code or committed files.
- For code changes, use the Karpathy Guidelines skill: make surgical changes and verify the affected gateway flow.
- Before deployment changes, verify Docker port behavior uses `PORT` from the environment.

## Intended constellation integration

The star-map experience should be added as a dashboard view over the gateway's existing memories. Map fragments, events, and core memories to visual layers first; only add richer entity or saga features after their data model is explicitly integrated into the gateway database.
