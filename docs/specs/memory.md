# B10 — Memory and session management (ops-ai `ops_ai/memory/`)

Covers B10.1 (session), B10.2 (short-term memory), B10.3 (long-term memory). Merchant tokens only.

## Identity and scope

- Every function takes a `Scope` built from the verified merchant token (`ops_ai/scope.py`). No request body,
  path parameter, Redis value or LLM output ever chooses the merchant.
- `SessionOwner(merchant_id: UUID, user_id: str)`; `owner_for(scope)` returns
  `SessionOwner(scope.merchant_id, f"merchant:{scope.merchant_id}")`. Gateway merchant tokens carry no operator
  id today (their `sub` is `merchant:<id>`), so the user slot holds the token subject. The key shape already has
  the slot so a per-operator `sub` drops in without a key migration.

## B10.1 Session store (`memory/session.py`)

- Constants: `SESSION_TTL_SECONDS = 1800`, `MAX_TURNS = 10`, `MAX_PENDING = 5`.
- `session_key(owner, session_id) -> str` = `ops_ai:session:{merchant_id}:{user_id}:{session_id}`.
- `SessionStore(redis: Redis, ttl_seconds: int = SESSION_TTL_SECONDS)`:
  - `create(owner, now) -> ChatSession`: new `session_id = uuid4()`, empty turns, empty state, no pending;
    written with `SET ... EX ttl_seconds`.
  - `load(owner, session_id) -> ChatSession | None`: reads only `session_key(owner, session_id)`. Returns `None`
    when the key is missing/expired, when the value does not parse, or when the stored `merchant_id`,
    `user_id` or `session_id` differs from the requested ones (ownership check, defence in depth).
  - `save(chat) -> None`: writes under the key derived from `chat.owner` and `chat.session_id`, refreshing the TTL
    to `ttl_seconds`; keeps only the last `MAX_TURNS` turns and the last `MAX_PENDING` pending proposals.
- Value = JSON (no pickle). Merchant B asking for merchant A's `session_id` computes a different key → `None`.

Dataclasses (frozen): `TimeWindow(start, end)`; `ConversationState(metric: Metric | None, window: TimeWindow |
None)`; `Turn(turn_id: UUID, question: str, intent: ChatIntent, metric: Metric | None, window: TimeWindow | None,
answer: str | None, at: datetime)`; `PendingMemory(proposal_id: UUID, content: str, source_turn_id: UUID)`;
`ChatSession(session_id: UUID, owner: SessionOwner, created_at: datetime, turns: tuple[Turn, ...], state:
ConversationState, pending: tuple[PendingMemory, ...])`. `EMPTY_STATE = ConversationState(None, None)`.

## B10.2 Short-term memory (`memory/state.py`, pure; `memory/conversation.py`)

- `Metric = Literal["failure_rate", "payment_volume"]`; `ChatIntent = Literal["metric_query",
  "memory_proposal", "unsupported"]`.
- `detect_metric(question) -> Metric | None` (case-insensitive): contains `fail`, `declin` or `error` →
  `failure_rate`; else contains `volume`, `how many payments`, `number of payments` or `payment count` →
  `payment_volume`; else `None`.
- `detect_window(question, now) -> TimeWindow | None` (UTC, `[start, end)`):
  `yesterday` → previous UTC day `[00:00 yesterday, 00:00 today)`; `today` → `[00:00 today, now)`;
  `last N hours` / `past N hours` (1 ≤ N ≤ 720) → `[now - N h, now)`; `last N days` / `past N days`
  (1 ≤ N ≤ 90) → `[now - N d, now)`; `last week` → `[now - 7 d, now)`; otherwise `None`. `yesterday` is checked
  before `today`.
- `detect_memory_request(question) -> str | None`: the question (stripped) starts with `remember that ` or
  `remember: ` (case-insensitive) → the rest, stripped; `None` unless `is_storable_memory(rest)`: 1..`MAX_MEMORY_CHARS`
  (500) chars, no hidden characters (Unicode Cc/Cf/Zl/Zp/Co/Cs), no run of 13+ digits joined by up to 3
  non-alphanumerics after NFKC (long-term rows outlive any log masking).
- `resolve(question, previous: ConversationState, now) -> tuple[ChatIntent, ConversationState]`:
  memory request → `("memory_proposal", previous)`. Otherwise `metric = detect_metric(q) or previous.metric`;
  no metric → `("unsupported", previous)`. `window = detect_window(q, now) or previous.window or
  TimeWindow(now - 24 h, now)` → `("metric_query", ConversationState(metric, window))`.
  So after "what is my card failure rate today?", "what about yesterday?" keeps `failure_rate` and moves the
  window to yesterday; the merchant is the scope of the call, never part of the text.
- `take_turn(db, store, chat, scope, question, now, llm) -> TurnResult`:
  - Precondition: `chat` was loaded with `owner_for(scope)`.
  - `metric_query`: one tool call `aggregate_failures(db, scope, window.start, window.end)` recorded as
    `ToolCall("aggregate_failures", {"start": iso, "end": iso, "metric": metric})`; evidence =
    `aggregate:window` summary, then one `memory:{memory_id}` item per recalled long-term memory, its summary prefixed with
    `MEMORY_LABEL` (merchant-stated, unverified, never a source for numbers).
  - `memory_proposal`: no tool runs and nothing is written; a `PendingMemory` is appended to the session and
    returned as `pending`, with `ToolCall("propose_merchant_memory", {"content": content})`.
  - `unsupported`: no tool call; text `None`, mode `t2_fallback`.
  - LLM (`metric_query` only) narrates `LlmRequest(question, evidence)`; `LlmUnavailableError` → `t2_fallback`.
  - Recalled memories (`recall_memories`) are returned on every turn as `memories`.
  - Appends the `Turn`, updates `state`, saves the session, returns
    `TurnResult(session, turn, aggregate, tool_calls, evidence, memories, pending, mode, text, model)`.
  - Proposals come from the user's own question text only. Evidence text, recalled memories and LLM output never
    create a proposal and never write `merchant_memory`.

## B10.3 Long-term memory (`memory/models.py`, `memory/store.py`)

Table `ops_ai.merchant_memory` (one Alembic revision): `memory_id uuid pk`, `merchant_id uuid not null`,
`created_by varchar(128) not null`, `content text not null` (check `char_length` 1..500), `source_session_id
uuid not null`, `source_turn_id uuid not null`, `supersedes_id uuid null`, `created_at`, `superseded_at null`,
`deleted_at null`, `deleted_by varchar(128) null`; index `(merchant_id, created_at)`; partial unique index
`(merchant_id, source_turn_id) WHERE supersedes_id IS NULL` (one confirmed row per proposal, ever). Rows are never
hard-deleted: a correction is a new row pointing at the old one (`supersedes_id`) and the old row gets
`superseded_at`; a delete sets `deleted_at`/`deleted_by` and overwrites the content of the row and its whole
correction chain with `[deleted]` (ids, times and actors stay for audit). Active = both `NULL`.
Confirm / correct / delete log one structured line each (merchant, memory id, actor; never the content).

- `MemoryRecord(memory_id, content, source_session_id, source_turn_id, supersedes_id, created_at)`.
- `save_confirmed_memory(db, scope, owner, session_id, proposal, now) -> MemoryRecord` (commits); `INSERT … ON CONFLICT DO
  NOTHING`, and `DuplicateMemoryError` when that source turn already has a row.
- `recall_memories(db, scope, limit=RECALL_LIMIT=20) -> list[MemoryRecord]`: active rows of the scope's merchant,
  newest first.
- `correct_memory(db, scope, owner, memory_id, content, now) -> MemoryRecord | None`: `None` when the id is
  unknown, belongs to another merchant, or is not active. The new row keeps the old `source_session_id` /
  `source_turn_id`.
- `delete_memory(db, scope, owner, memory_id, now) -> bool`: same `None`/`False` rules.
- `confirm_memory(db, store, chat, scope, proposal_id, now) -> MemoryRecord | None` (in `conversation.py`):
  the only writer of new memories. Looks the proposal up in `chat.pending`; unknown → `None`. On success the
  row is written first, then the proposal is removed from the session. A second or concurrent confirm, or a
  proposal revived by a racing turn's save, hits the unique index → `None`, so a deleted memory never comes back.

## HTTP (merchant token; every response `Cache-Control: no-store`)

Mounted by `create_app` when `OpsAiAppDeps.redis` is set (`main.py` passes its client).

| Route | Result |
|---|---|
| `POST /v1/assistant/sessions` | 201 `{session_id, expires_in_seconds}` |
| `GET /v1/assistant/sessions/{session_id}` | 200 `{session_id, state: {metric, window}, turns: [{turn_id, question, intent, metric, window}]}` |
| `POST /v1/assistant/sessions/{session_id}/turns` `{question}` | 200 `{session_id, turn_id, intent, metric, window, mode, text, model, result: {total, failed, rate} \| null, tool_calls, evidence, memories: [{memory_id, content, source_turn_id}], pending_memory: {proposal_id, content} \| null}` |
| `POST /v1/assistant/sessions/{session_id}/memory-proposals/{proposal_id}/confirm` | 201 `{memory_id, content, source_session_id, source_turn_id, supersedes_id, created_at}` |
| `GET /v1/assistant/memories` | 200 `{memories: [...]}` (same item shape as confirm) |
| `PATCH /v1/assistant/memories/{memory_id}` `{content}` | 200 the new row |
| `DELETE /v1/assistant/memories/{memory_id}` | 204 |

Bodies are `extra="forbid"` (a `merchant_id` field → 422); `question` 1..2000 chars, `content` 1..500 chars after stripping, no hidden
characters or card numbers (same rule).
Unknown, expired or another merchant's session / proposal / memory → the identical 404 (`session not found`,
`proposal not found`, `memory not found`). User (consumer) token → 403; missing/invalid token → 401.
