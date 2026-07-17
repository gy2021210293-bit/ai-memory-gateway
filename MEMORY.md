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
- The constellation is a read-only `/constellation` page. It reuses the original project's canvas renderer and visual styling while consuming the gateway's `/api/memories`; visualization does not maintain a separate memory database.
- Entity MVP uses PostgreSQL `entities`, `entity_aliases`, and `memory_entities` as the canonical entity layer. New extracted memories carry explicit named entities into these tables; legacy memories are only backfilled through the bounded manual Dashboard action, with `memories.entity_scanned` preventing repeat charges for entity-free records. Retrieval prompt entries include linked entity names, and the star map now treats entities as constellations across People, Places, Projects, Events, and Life galaxies.

## Open questions

- Entity descriptions and automatic alias discovery are intentionally deferred until entity extraction and manual merge behavior are validated on deployed data.
