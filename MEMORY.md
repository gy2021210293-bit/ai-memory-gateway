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
- Retrieval is entity-aware: `search_memories()` combines keyword candidates, optional vector candidates, and entity name/alias candidates. Entity hits recall linked active memories across layers 1/2/3, add `MEMORY_HW_ENTITY` to ranking, attach entity metadata to results, and inject one deduplicated matched-entity overview before the recalled memories.
- Entity profiles are manual and evidence-backed: Dashboard draft generation reads up to 80 active linked memories, returns structured fields without writing, shows old/new profiles side by side, and only persists after confirmation. Saved profiles include evidence IDs, model, and update time; unsupported evidence IDs are rejected, and structured profile fields are included in entity-aware retrieval context.
- The user is not an entity. `USER_ENTITY_NAMES` controls self-name exclusions; extraction, legacy backfill, and database linking reject those names, and startup removes existing matching entity rows without deleting memories. Entity summaries are compact AI-first-person current views: summary <=200 characters, supporting lists <=6 items, the Dashboard draft summary is editable before confirmation, and saving replaces the prior profile on the same entity.
- Fragment, summary, entity-profile, and consolidated-event prompts identify the AI as `我是栖` and request 栖's first-person `我`; narrative voice is a generation-quality target, not a persistence gate, so third-person output remains stored for manual correction instead of being lost.
- The runtime system-prompt loader narrowly migrates only legacy leading identities `我是向野。` and `你是Huxley。` to `我是栖。`, including the database-backed prompt that takes precedence over `system_prompt.txt`; the remainder of customized prompt text is preserved.
- Fragment extraction resolves relative time from each persisted conversation message's `created_at` in `TIMEZONE_HOURS`, writes the absolute date directly into memory `content`, preserves the source expression's precision and plan/possibility status, and does not add structured temporal storage yet.
- Consolidated events include their absolute date naturally in `content` and return a per-event `event_date` saved through the existing database column; consolidation provides full local source timestamps and falls back to the selected range start only when the model date is invalid.
- Consolidation responses are decoded with `llm_json.parse_json_array()` instead of a greedy first-`[`/last-`]` regex; bracketed model commentary, code fences, unescaped control characters, and trailing commas no longer invalidate an otherwise usable event array, and AI repair receives the complete response.
- Fragment archival follows successful event persistence: only validated in-range IDs from a created event are deactivated. Rerunning consolidation first reactivates date-range layer-1 fragments that are inactive but not referenced by any layer-2 event, repairing orphaned fragments from the former archive-all behavior.
- The cognitive-model MVP uses one PostgreSQL `cognitive_items` table with `user`, `self`, and `relationship` subjects. Items are created and edited manually in Dashboard, may cite validated memory IDs, and only active confirmed items are injected into both normal and partition-cached chat paths. Each item is capped at 160 characters; injection uses a fair round-robin selection of at most four items per subject and a 1,200-character total budget. Automatic inference, background updates, and personality mutation remain deferred.
- The constellation binary core represents the two participants rather than memory layers. Its default labels are `晏晏` and `栖`, configurable through `UI_USER_NAME` and `UI_AI_NAME`; event and core memories remain supporting content for those two stars.

## Open questions

- Automatic alias discovery remains deferred until manual entity merge behavior is validated on deployed data.
