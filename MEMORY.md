# Project Memory

## Project facts

- Project: AI Memory Gateway.
- Stack: Python 3.12, FastAPI, asyncpg/PostgreSQL, Jinja2 dashboard, OpenAI-compatible upstream APIs.
- Deployment: already modified by the user and successfully deployed on Zeabur.
- Main capability: gateway-owned closed loop of conversation capture, memory extraction, retrieval, consolidation, and prompt injection.

## Decisions

- Treat this gateway and its PostgreSQL database as the sole source of truth for conversations and memories.
- Add the Memory Constellations experience as a dashboard visualization module, not as a second memory gateway or parallel database.
- SiliconFlow can be used through its OpenAI-compatible APIs for both chat and embeddings; semantic retrieval should remain attached to the gateway's existing vector-search path.
- The constellation is a read-only `/constellation` page. It reuses the original project's canvas renderer and visual styling, while a gateway adapter consumes `/api/memories`: layer 1 fragments form session/date constellations, layer 2 events form event constellations, and layer 3 memories appear in the core view. It does not alter the memory pipeline or database schema.

## Open questions

- Inspect the user's custom changes and deployed configuration before selecting the first constellation schema mapping and UI integration point.
