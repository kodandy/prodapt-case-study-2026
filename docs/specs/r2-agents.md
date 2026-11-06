# R2 agentic items — test specification (11-05)

Covers R2-1 (multi-agent orchestration), R2-4 (incident reranking), R2-5 (bulk token optimisation), R2-6
(agent-to-agent messages, SQ2 by fraud + compliance + settlement) and R2-7 (feedback loop). All code lives in
`services/ops-ai/src/ops_ai/{agents,bulk,feedback}/` and `ops_ai/search/rerank.py`. Every tool an agent calls is
read-only; only the audit trail, feedback, scores and rule versions are written. The scope always comes from the
token, never from the body or from model output.

## 0. Migration `c7e3a1d9b4f2` (revises `b12c4e7a9f31`), schema `ops_ai`

| table | columns |
|---|---|
| `agent_messages` | `message_id` UUID PK, `correlation_id` UUID (indexed), `sequence` int, `sender` varchar(32), `recipient` varchar(32), `intent` varchar(48), `payload` JSONB, `evidence_refs` JSONB (list of str), `merchant_id` UUID null (indexed), `user_id` UUID null, `created_at` timestamptz. Unique (`correlation_id`, `sequence`). |
| `kyc_profiles` | `user_id` UUID PK, `kyc_status` varchar(16) CHECK in (`verified`,`pending`,`failed`), `country` varchar(2), `ingest_source` varchar(16) |
| `feedback` | `feedback_id` UUID PK, `merchant_id` UUID, `target_kind` varchar(16) CHECK in (`alert`,`answer`,`incident`), `target_id` varchar(128), `verdict` varchar(16) CHECK in (`true_positive`,`false_positive`,`helpful`,`not_helpful`), `comment` text null, `created_at`. Unique (`merchant_id`,`target_kind`,`target_id`). |
| `fraud_rule_versions` | PK (`merchant_id` UUID, `version` int), `steps` int CHECK 0..5, `min_micro_payments` int, `min_micro_share` float, `min_distinct_cards` int, `reason` varchar(64), `feedback_id` UUID null, `created_at` |
| `incident_feedback_scores` | PK (`merchant_id` UUID, `incident_id` varchar(32)), `helpful` int default 0, `not_helpful` int default 0, `updated_at` |

Downgrade drops all five. Models: `ops_ai/agents/models.py` (`AgentMessageRecord`, `KycProfile`),
`ops_ai/feedback/models.py` (`Feedback`, `FraudRuleVersion`, `IncidentFeedbackScore`), registered in
`ops_ai/schema.py`.

## 1. Feedback and fraud thresholds — `ops_ai/feedback/` (R2-7)

`thresholds.py`
```python
MAX_STEPS = 5
@dataclass(frozen=True)
class RuleVersion:
    version: int            # 0 = shipped baseline (no row)
    steps: int
    min_micro_payments: int
    min_micro_share: float
    min_distinct_cards: int
    reason: str             # "baseline" for version 0
    feedback_id: uuid.UUID | None
    created_at: datetime | None   # None for version 0
BASELINE: RuleVersion  # version 0, steps 0, 10, 0.8, 3, "baseline", None, None
def thresholds_for_steps(steps: int) -> tuple[int, float, int]
    # steps clamped to [0, MAX_STEPS]; (10 + 2*steps, round(min(0.8 + 0.03*steps, 0.95), 2), 3 + steps)
    # steps 5 -> (20, 0.95, 8). Never looser than the baseline.
def active_rule_version(session, scope: Scope) -> RuleVersion   # newest version row of the merchant, else BASELINE
def rule_history(session, scope: Scope) -> list[RuleVersion]    # the merchant's rows, newest first (no baseline)
def is_flagged(episode: CardTestingEpisode, rule: RuleVersion) -> bool
    # micro_payments >= min_micro_payments and micro_share >= min_micro_share
    # and (distinct_cards is None or distinct_cards >= min_distinct_cards)
def revert_rule(session, scope, clock, version: int) -> RuleVersion | None
    # None when version > 0 and the merchant has no such row. Otherwise inserts a NEW row
    # (active.version + 1) with the target's steps/values (version 0 -> baseline values),
    # reason f"revert:v{version}", feedback_id None, created_at clock(). Returns the new row. Does not commit.
```
Thresholds are per merchant: one merchant's feedback never changes another merchant's rules.

`service.py`
```python
TargetKind = Literal["alert", "answer", "incident"]
Verdict = Literal["true_positive", "false_positive", "helpful", "not_helpful"]
VERDICTS_BY_KIND = {"alert": ("true_positive", "false_positive"),
                    "answer": ("helpful", "not_helpful"), "incident": ("helpful", "not_helpful")}
@dataclass(frozen=True) class IncidentScore: incident_id: str; helpful: int; not_helpful: int
class FeedbackRejectedError(Exception): reason: Literal["verdict_mismatch", "unknown_target", "duplicate"]
@dataclass(frozen=True)
class FeedbackResult:
    feedback_id: uuid.UUID; target_kind: TargetKind; target_id: str; verdict: Verdict
    rule_version: RuleVersion | None      # the new row when this feedback changed the thresholds
    incident_score: IncidentScore | None  # the updated counts for incident feedback
def record_feedback(session, scope, clock, target_kind, target_id, verdict, comment: str | None) -> FeedbackResult
def incident_scores(session, scope, incident_ids: Sequence[str]) -> dict[str, IncidentScore]  # only ids with a row
```
`record_feedback` (does not commit; checks in this order):
1. verdict not in `VERDICTS_BY_KIND[target_kind]` → `verdict_mismatch`.
2. `alert`: `target_id` must be a UUID of a `fraud_alerts` row of the scope merchant, else `unknown_target`.
   `incident`: an `incidents` row whose `merchant_id` is the scope merchant or NULL, else `unknown_target`.
   `answer`: any id (an opaque answer / correlation id).
3. existing feedback for (merchant, kind, target_id) → `duplicate` (one vote per target: bounded influence).
4. Effects: `false_positive` → steps + 1, `true_positive` → steps − 1, clamped to [0, MAX_STEPS], starting from the
   active version's steps. A new version row (active.version + 1, reason `feedback:false_positive` /
   `feedback:true_positive`, `feedback_id` set) is written only when the steps change; `rule_version` is then that row,
   else None. `helpful` / `not_helpful` on an incident upserts `incident_feedback_scores` (+1 on the matching
   counter). Answer feedback is stored only.

## 2. Incident reranking — `ops_ai/search/rerank.py` (R2-4)

```python
RECENCY_HALF_LIFE_DAYS = 30.0
WEIGHT_RELEVANCE, WEIGHT_RECENCY, WEIGHT_EFFECTIVENESS = 0.5, 0.2, 0.3
REOPEN_PENALTY = 0.5
@dataclass(frozen=True)
class IncidentCandidate:
    incident_id: str; title: str; root_cause: str; fix_applied: str
    started_at: datetime; resolved_at: datetime | None; recurrence_within_7d: bool
    relevance: float; helpful: int = 0; not_helpful: int = 0
@dataclass(frozen=True)
class RankedIncident:
    incident_id: str; title: str; root_cause: str; fix_applied: str
    started_at: datetime; resolved_at: datetime | None; recurrence_within_7d: bool
    relevance: float      # candidate relevance / max relevance of the batch (0.0 when the max is 0)
    recency: float; effectiveness: float; score: float
def effectiveness(resolved: bool, recurrence_within_7d: bool, helpful: int, not_helpful: int) -> float
    # 0.0 when not resolved; else (helpful + 1) / (helpful + not_helpful + 2), times REOPEN_PENALTY when it recurred
def recency(at: datetime, now: datetime) -> float   # 0.5 ** (max(age_days, 0) / RECENCY_HALF_LIFE_DAYS)
def rerank_incidents(candidates: Sequence[IncidentCandidate], now: datetime) -> list[RankedIncident]
    # recency of resolved_at, or started_at when unresolved;
    # score = 0.5*relevance + 0.2*recency + 0.3*effectiveness; best score first, ties by incident_id ascending
def search_incidents_reranked(session, scope, query, now, embedder, limit=5) -> list[RankedIncident]
```
`search_incidents_reranked` is hybrid first, rerank second: a keyword leg (FTS `websearch_to_tsquery` over the
`incidents` the scope can see, by `ts_rank`, up to 30) and a vector leg (the embedder over the query and each visible
incident's `title root_cause fix_applied`, newest 200 by `started_at`, cosine ≥ 0.4), fused with
`reciprocal_rank_fusion`; the fused score is the candidate relevance, the merchant's `incident_feedback_scores` fill
helpful / not_helpful, then `rerank_incidents`, top `limit`. Another merchant's incidents never appear; an
unavailable embedder degrades to the keyword leg.

### 2.1 Candidates from the indexed incident documents (R2-4 follow-up, 11-06)

Supersedes the two legs above for every incident that has a `search_documents` row (sq1.md §10.1). The rerank
(`rerank_incidents`, the scope merchant's scores, best score first, ties by `incident_id`) is unchanged.

- **Indexed incidents**: one `hybrid_search(session, scope, query, embedder, limit=FUSED_CANDIDATES (60),
  kinds=("incident",), include_platform=True)`. Its vector leg uses the stored vector of the embedder's column; the
  embedder is called with the query only, never with an incident text. Each hit is mapped back through
  `incident_ref_id` and kept only if that incident is visible to the scope in `ops_ai.incidents` (own or
  platform-wide) — a stale or mis-owned document never widens visibility. Relevance = the hit's RRF score.
- **Un-indexed fallback**: visible incidents with no `kind = "incident"` document (`ref_id = incident_ref_id(id)`)
  owned by the scope merchant or `PLATFORM_MERCHANT_ID` (i.e. none `hybrid_search` could return for this scope) keep the pre-indexing path of section 2, restricted to them: FTS over `ops_ai.incidents` (≤ 30) and per-request
  embedding of their `title root_cause fix_applied` (newest ≤ 200), fused with `reciprocal_rank_fusion` (k = 60).
  Their RRF score is on the same scale as the indexed hits' (two legs, k = 60); both lists are merged by incident id
  (an incident is in only one of them) before the rerank. When every visible incident is indexed, no incident text
  is embedded.
- Another merchant's incident never appears, whether indexed (owner filter of `hybrid_search` + visibility re-check)
  or not; platform-wide incidents only through the incident kind.
- An unavailable embedder degrades both lists to their keyword legs.

Tests (RED first, `search/__tests__/test_search_incidents_reranked_indexed.py`): indexed incidents → every embedder
call is `[query]`; scores / recurrence ordering hold on indexed incidents; mixed indexed + un-indexed → both returned,
only the un-indexed text is embedded; cross-tenant: another merchant's indexed incident (same text) never appears, nor
does a document owned by the scope merchant whose ref points to another merchant's incident; platform-wide indexed
incident appears; an own incident whose only document is owned by another merchant is still found (fallback); an
unavailable embedder still returns indexed keyword hits.

## 3. Bulk transaction analysis — `ops_ai/bulk/` (R2-5)

`tokens.py`: `count_tokens(text: str) -> int` = `len(re.findall(r"\w+|[^\w\s]", text))` (deterministic, no
tokenizer dependency).

`analysis.py`
```python
DEFAULT_TOKEN_BUDGET = 800; DEFAULT_CHUNK_TOKENS = 2000; TOP_ITEMS = 5
@dataclass(frozen=True)
class TxRow: payment_id: uuid.UUID; amount_minor: int; currency: str; method: str; state: str
             decline_code: str | None; card_bin: str | None; occurred_at: datetime
def render_row(row: TxRow) -> str
    # f"{payment_id} {occurred_at.isoformat()} {method} {state} {amount_minor} {currency} bin={card_bin or '-'} code={decline_code or '-'}"
def naive_prompt(rows: Sequence[TxRow]) -> str   # "Transactions:\n" + one render_row per line
@dataclass(frozen=True)
class ChunkSummary:
    rows: int
    amount_by_currency: dict[str, int]
    by_state: dict[str, int]; by_method: dict[str, int]
    decline_codes: dict[str, int]          # failed rows (state in declined/failed) with a code
    failed_by_bin: dict[str, int]          # failed rows with a BIN
def chunk_rows(rows, max_tokens_per_chunk: int) -> list[list[TxRow]]
    # greedy, order kept; a chunk holds rows while the sum of count_tokens(render_row) stays <= the max,
    # a single row larger than the max is its own chunk; [] for no rows; ValueError when max <= 0
def summarize_chunk(rows: Sequence[TxRow]) -> ChunkSummary           # map
def merge_summaries(summaries: Sequence[ChunkSummary]) -> ChunkSummary  # reduce: counter-wise sums
def render_summary(summary: ChunkSummary, top_items: int = TOP_ITEMS) -> str
    # rows, amount per currency, by state, by method, top `top_items` decline codes and failing BINs
    # (count desc, then key asc); top_items 0 leaves the two top lists out
@dataclass(frozen=True)
class BulkAnalysis:
    transactions: int; chunks: int; naive_tokens: int; optimized_tokens: int
    reduction: float      # 1 - optimized/naive, 0.0 when naive is 0
    within_budget: bool; summary: ChunkSummary; prompt: str
def analyze_bulk(rows, token_budget=DEFAULT_TOKEN_BUDGET, chunk_tokens=DEFAULT_CHUNK_TOKENS) -> BulkAnalysis
    # map-reduce; prompt = render_summary(merged, k) for the largest k in TOP_ITEMS..0 whose tokens fit the budget
    # (k = 0 when none fits); within_budget = optimized_tokens <= token_budget
```
`service.py`: `MAX_BULK_ROWS = 20000`; `load_rows(session, scope, start, end) -> list[TxRow]` (the merchant's
`payment_facts` in [start, end), oldest first, at most MAX_BULK_ROWS);
`analyze_transactions(session, scope, start, end, llm, token_budget) -> BulkResult` with `analysis`, `mode`, `text`,
`model`; the LLM gets one evidence item `EvidenceItem(ref="bulk:summary", summary=analysis.prompt)` and never the
raw rows; `LlmUnavailableError` → `t2_fallback`, text None.

## 4. Agents and messages — `ops_ai/agents/` (R2-1, R2-6)

`messages.py`
```python
AgentName = Literal["supervisor", "fraud", "settlement", "dispute", "compliance"]
MessageIntent = Literal["investigate", "assess_transfer", "check_kyc", "verify_funds", "funds_verified",
    "check_disputes", "chargeback_impact", "chargeback_impact_result", "finding", "root_cause"]
JsonScalar = str | int | float | bool | None
@dataclass(frozen=True)
class AgentMessage:
    message_id: uuid.UUID; correlation_id: uuid.UUID; sequence: int
    sender: AgentName; recipient: AgentName; intent: MessageIntent
    payload: Mapping[str, JsonScalar]; evidence_refs: tuple[str, ...]; created_at: datetime
def message_to_json(message) -> str; def message_from_json(raw: str) -> AgentMessage   # round-trip equal
```
`bus.py`
```python
MAX_MESSAGES = 16
class MessageTransport(Protocol): def publish(self, message: AgentMessage) -> None: ...
class RedisStreamTransport:   # optional transport: XADD <stream> MAXLEN ~ maxlen  {"message": message_to_json(m)}
    def __init__(self, redis: Redis, stream: str = "ops_ai:agent_messages", maxlen: int = 10000)
class MessageBudgetExceededError(Exception)
class InProcessBus:
    def __init__(self, correlation_id, clock, max_messages=MAX_MESSAGES, transport: MessageTransport | None = None)
    def send(self, sender, recipient, intent, payload, evidence_refs=()) -> AgentMessage
        # sequence 1, 2, ...; queued FIFO; published to the transport when set;
        # the (max_messages + 1)-th send raises MessageBudgetExceededError and is not queued
    def next_message(self) -> AgentMessage | None   # FIFO, None when empty
    @property def history(self) -> tuple[AgentMessage, ...]   # every sent message in order
def persist_messages(session, messages, merchant_id: uuid.UUID | None, user_id: uuid.UUID | None) -> int
def load_messages(session, scope: Scope, correlation_id) -> list[AgentMessage]   # by sequence; [] for another merchant
```
`tools.py`: per-agent allowlists; a tool outside the list raises `ToolNotAllowedError` before any query.
```python
AGENT_TOOLS: dict[AgentName, frozenset[str]] = {
    "supervisor": frozenset(),
    "fraud": frozenset({"analyze_fraud", "fraud_rule_version", "transfer_status"}),
    "settlement": frozenset({"explain_settlement", "transfer_status"}),
    "dispute": frozenset({"dispute_tickets", "explain_settlement"}),
    "compliance": frozenset({"kyc_profile", "transfer_status"}),
}
class ToolNotAllowedError(Exception)
class AgentToolbox:
    def __init__(self, agent: AgentName, session: Session)
    tool_calls: list[ToolCall]  # every allowed call, in order (ops_ai.investigation.service.ToolCall)
    def analyze_fraud(self, scope, start, end) -> FraudAnalysis
    def fraud_rule_version(self, scope) -> RuleVersion
    def explain_settlement(self, scope, period: str | None) -> SettlementStatement | None
    def dispute_tickets(self, scope) -> DisputeTickets   # counts of the merchant's source='dispute' tickets by status
    def transfer_status(self, scope: UserScope, transfer_id) -> TransferStatus | None
    def kyc_profile(self, scope: UserScope) -> KycProfileView | None
```
`kyc.py`: `KycProfileView(user_id, kyc_status, country)`; `get_kyc_profile(session, scope: UserScope)`;
`seed_kyc_profiles(session_factory, log: ScenarioLog) -> int` — one row per p2p sender (first record wins,
`ingest_source "seed"`, ON CONFLICT DO NOTHING, re-run returns 0). `python -m ops_ai.agents` seeds them and prints
`kyc_profiles: <n> inserted`; the compose `seed` service runs it.

`AgentContribution(agent: AgentName, status: Literal["finding", "no_finding"], summary: str,
evidence: tuple[EvidenceItem, ...], tool_calls: tuple[ToolCall, ...])` — one per agent that handled a message.

### 4.1 Merchant investigation (R2-1) — `supervisor.py`
`plan_agents(question) -> tuple[AgentName, ...]`: keywords (case-insensitive) — fraud: `fraud`, `card testing`,
`card-testing`, `micro`, `suspicious`; settlement: `payout`, `settlement`, `fee`, `deduct`; dispute: `dispute`,
`chargeback`. Order fraud, settlement, dispute; none matched → all three.

`investigate_with_agents(session, scope: Scope, question, now, llm, transport=None) -> AgentInvestigation`:
the supervisor sends `investigate` to each planned agent, then delivers messages FIFO until the queue is empty or the
budget is hit (`truncated=True`).
- fraud on `investigate`: `analyze_fraud` over [now − 24 h, now) and `fraud_rule_version`; an episode is flagged by
  `is_flagged(episode, active rule)`. Flagged → `finding` (evidence `episode:<first_payment_id>`, summary names the rule
  version) and a `check_disputes` to dispute; none → `no_finding`. Sends `finding` to supervisor.
- dispute on `investigate` or `check_disputes` (only the first is worked): `dispute_tickets`; sends
  `chargeback_impact` to settlement. On `chargeback_impact_result`: contribution (`finding` when there are dispute
  tickets or chargebacks, evidence `disputes:<merchant_id>`), `finding` to supervisor.
  It also calls the dispute judge on the merchant's dispute workflow records (rubric only) and adds the verdict
  to its contribution and `finding` payload: docs/specs/evaluation-judge.md §6.4.1.
- settlement on `investigate`: `explain_settlement(latest)`; `finding` when gap ≠ 0, evidence `statement:<period>`;
  none → `no_finding`. On `chargeback_impact`: reads the same statement and replies `chargeback_impact_result` with
  `chargebacks_minor`, `chargeback_fees_minor`, `period` (0 / None without a statement).
- `AgentInvestigation(correlation_id, plan, contributions, messages, finding, evidence, tool_calls, truncated, mode,
  text, model)`; `finding` = the contribution summaries joined in plan order; the LLM narrates the combined evidence
  (`LlmUnavailableError` → `t2_fallback`). Messages are persisted with the merchant id.

### 4.2 Transfer root cause (R2-6, SQ2) — `rca.py`
`investigate_transfer_rca(session, scope: UserScope, question, transfer_id, now, llm, transport=None)
-> TransferRca | None` (None when the scope has no such transfer; same lookup as `get_payment_status`).
Message trail for a failed transfer, exactly:

| seq | sender → recipient | intent | payload keys |
|---|---|---|---|
| 1 | supervisor → fraud | assess_transfer | transfer_id |
| 2 | fraud → compliance | check_kyc | transfer_id, fraud_hold (false) |
| 3 | fraud → supervisor | finding | fraud_hold |
| 4 | compliance → settlement | verify_funds | transfer_id |
| 5 | settlement → compliance | funds_verified | balance_covered_amount |
| 6 | compliance → supervisor | root_cause | cause_code, remediation_owner, kyc_status, kyc_tier, cross_border |

Not a failed transfer → only messages 1 and 3 (`fraud_hold` false) and `cause_code "not_declined"`. A raw `flagged`
status (fraud hold) is never revealed: it is treated like any non-failed transfer. No payload or evidence carries
`rule_id` or `reason_code`. `TransferRca(correlation_id, cause_code, remediation_owner, contributions (fraud,
compliance, settlement), messages, suggested_fixes, evidence, truncated, mode, text, model)`; cause from
`classify_transfer`; fix = the KYC tier upgrade when the next tier's limit covers the amount (as
`investigate_transfer`). Planted SQ2 → `cross_border_limit_for_kyc_tier`, owner `sender`, balance covered.
Messages are persisted with the user id.

## 5. Routes (registered in `create_app`)
All JSON, `Cache-Control: no-store` on agent / feedback / bulk / search responses, 401/403 as the other routes.
- `POST /v1/agents/investigate` (merchant) `{question}` → `{correlation_id, plan, finding, contributions:[{agent,
  status, summary, evidence:[{ref, summary}], tool_calls:[{name, args}]}], messages:[{sequence, sender, recipient,
  intent, payload, evidence_refs}], evidence, truncated, mode, text, model}`.
- `GET /v1/agents/messages/{correlation_id}` (merchant) → `{messages:[...]}`; unknown or another merchant's → 404.
- `POST /v1/agents/transfer-rca` (user) `{question, transfer_id?}` (extra fields 422) → `{correlation_id,
  cause_code, remediation_owner, contributions, messages, suggested_fixes, evidence, truncated, mode, text, model}`;
  missing / another sender's transfer → 404 `{"detail": "transfer not found"}`.
- `POST /v1/incidents/search` (merchant) `{query (1..500), limit (1..20, default 5)}` → `{results:[{incident_id,
  title, root_cause, fix_applied, resolved_at, recurrence_within_7d, relevance, recency, effectiveness, score}]}`.
- `POST /v1/bulk/analyze` (merchant) `{start, end, token_budget (100..4000, default 800)}`; end ≤ start or a span over
  31 days → 422 → `{transactions, chunks, naive_tokens, optimized_tokens, reduction, within_budget, prompt, mode,
  text, model}`.
- `POST /v1/feedback` (merchant) `{target_kind, target_id (1..128), verdict, comment? (≤500)}` → 201 `{feedback_id,
  target_kind, target_id, verdict, rule_version: RuleVersionOut | null, incident_score: {incident_id, helpful,
  not_helpful} | null}`; verdict_mismatch → 422 `{"detail": "verdict does not match target"}`; unknown_target → 404
  `{"detail": "target not found"}`; duplicate → 409 `{"detail": "feedback already recorded"}`.
- `GET /v1/feedback/fraud-rules` (merchant) → `{active: RuleVersionOut, history: [RuleVersionOut]}`;
  `RuleVersionOut = {version, steps, min_micro_payments, min_micro_share, min_distinct_cards, reason, feedback_id,
  created_at}`.
- `POST /v1/feedback/fraud-rules/revert` (merchant) `{version (>= 0)}` → 201 RuleVersionOut; unknown → 404
  `{"detail": "rule version not found"}`.

## 6. Live watcher thresholds and rule actors (R2-7 follow-up, 11-05)

Closes two R2-7 gaps: the live fraud watcher `watch_payment` used only the fixed baseline, and threshold versions did
not record who acted. Purely additive: no existing table, column, response shape or dataclass changes (the existing
`fraud_rule_versions` / `fraud_alerts` column sets, `RuleVersion`, `RuleVersionOut` and `FraudRulesOut` stay as they
are), so a merchant without a rule version sees byte-identical alerts, tickets, ops alerts and log lines.

### 6.0 Migration `d2b7e4a9c1f6` (revises `c4f1a8e2b7d3`, the single head before it), schema `ops_ai`

| table | columns |
|---|---|
| `fraud_rule_version_actors` | PK (`merchant_id` UUID, `version` int); FK (`merchant_id`, `version`) → `fraud_rule_versions` (`merchant_id`, `version`) ON DELETE CASCADE; `actor` varchar(64) not null; `actor_role` varchar(32) not null CHECK in (`merchant_admin`,`merchant_support`); `created_at` timestamptz not null |
| `fraud_alert_rule_versions` | `alert_id` UUID PK, FK → `fraud_alerts.alert_id` ON DELETE CASCADE; `merchant_id` UUID not null; `rule_version` int not null CHECK `rule_version >= 0`; `applied_at` timestamptz not null |

Downgrade (to `c4f1a8e2b7d3`) drops both; upgrade restores them. Models: `FraudRuleVersionActor` in
`ops_ai/feedback/models.py`, `FraudAlertRuleVersion` in `ops_ai/fraud_watch/models.py` (already registered modules in
`ops_ai/schema.py`); `alembic check` reports no drift. After this migration there is still exactly one head.

### 6.1 Acting principal on every threshold version — `ops_ai/feedback/thresholds.py`

```python
def actor_of(scope: Scope) -> str          # f"merchant:{scope.merchant_id}" (the token subject; merchant tokens
                                            # carry no per-user id, so the principal is merchant + role)
@dataclass(frozen=True)
class RuleAuditEntry:
    version: int
    reason: str
    feedback_id: uuid.UUID | None
    actor: str | None        # None only for a version row without an actor row (e.g. written before 6.0)
    actor_role: str | None
    created_at: datetime
def rule_audit(session, scope: Scope) -> list[RuleAuditEntry]
    # the scope merchant's version rows LEFT JOIN their actor rows, newest version first; no baseline entry;
    # never another merchant's rows
```
- Every new version row written through `append_rule_version` — i.e. by `record_feedback` (false / true positive that
  moves the steps) and by `revert_rule` — also inserts one `fraud_rule_version_actors` row in the same transaction with
  the same (`merchant_id`, `version`), `actor = actor_of(scope)`, `actor_role = scope.role`, `created_at = clock()`.
  No actor row is written when no version row is written (a true positive at the baseline, a false positive at
  `MAX_STEPS`, a revert to an unknown version → None).
- The acting principal is the scope passed in, which routes build from the token only. A `merchant_support` token
  that posts feedback or reverts records `actor_role = "merchant_support"`.
- Signatures of `record_feedback`, `revert_rule`, `append_rule_version`, `active_rule_version`, `rule_history` and
  the `RuleVersion` fields are unchanged; rolling back the transaction removes both the version and its actor row.

Route (merchant token, `Cache-Control: no-store`, 401/403 as the other merchant routes):
- `GET /v1/feedback/fraud-rules/audit` → 200 `{entries: [{version, reason, feedback_id, actor, actor_role,
  created_at}]}` from `rule_audit`, newest first; `{entries: []}` for a merchant with no versions.

### 6.2 Live watcher reads the active version — `ops_ai/fraud_watch/watch.py`

```python
def watch_payment(session, clock, event: PaymentEventV1) -> uuid.UUID | None   # signature unchanged
def applied_rule_version(session, scope: Scope, alert_id: uuid.UUID) -> int | None
    # the rule_version recorded for the scope merchant's alert; None when the alert has no record or belongs to
    # another merchant
```
- Unchanged: the live / micro gate, the `analyze_fraud` window `[occurred_at - 30 min, occurred_at + 1 µs)` and the
  choice of the episode holding `event.payment_id`.
- New: `rule = active_rule_version(session, Scope(merchant_id=event.merchant_id))`, read in the same transaction.
  The episode is flagged iff `episode.flagged and is_flagged(episode, rule)`: the active version can only tighten the
  live watcher, never loosen it below the shipped baseline (a hand-edited looser row has no effect). Not flagged →
  None, and no alert, ticket, ops alert or `fraud_alert_rule_versions` row is written.
- Flagged → exactly as before (`fraud_alerts` upsert with the same `features`, ticket and ops alert on first insert,
  the same single log line), plus an upsert of `fraud_alert_rule_versions` for the returned alert id:
  insert (`alert_id`, `merchant_id = event.merchant_id`, `rule_version = rule.version`, `applied_at = clock()`), or on
  conflict (`alert_id`) update `rule_version` and `applied_at` to the values of this evaluation. Version 0 is recorded
  for a merchant on the baseline.
- Examples (12 micro USD events 10 s apart, 4 distinct cards, the SQ3 live burst): baseline → alert, applied version
  0; active version with steps 1 (12, 0.83, 4) → alert, applied version = that version number; active version with
  steps 2 (14, 0.86, 5) → no alert, no ticket, no ops alert; a revert to version 0 after that → the next burst raises
  an alert with applied version = the revert row's version number. One merchant's version never changes another
  merchant's live alerting.
- The SQ3 eval golden is unchanged (seeding writes no rule versions).

## 7. Keyset paging of the rule history and audit (R2-7 follow-up, 11-06)

Closes the R2-7 residual "history is unpaged": `GET /v1/feedback/fraud-rules` and `GET /v1/feedback/fraud-rules/audit`
returned every version row of the merchant. No migration (the primary key (`merchant_id`, `version`) is the keyset),
no change to `rule_history`, `rule_audit`, `RuleVersion`, `RuleAuditEntry`, `RuleVersionOut`, `RuleAuditEntryOut`,
`FraudRulesOut` or `FraudRuleAuditOut`.

### 7.1 Cursor codec — `ops_ai/feedback/paging.py`

```python
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_CURSOR_LENGTH = 256
CursorKind = Literal["history", "audit"]
class InvalidCursorError(Exception)
def encode_cursor(kind: CursorKind, merchant_id: uuid.UUID, version: int) -> str
    # unpadded base64url of f"{kind}:{merchant_id}:{version}"; opaque to clients
def decode_cursor(cursor: str, kind: CursorKind, merchant_id: uuid.UUID) -> int
    # the version to continue below; InvalidCursorError unless the value is exactly what encode_cursor returns for
    # this kind and this merchant with a version >= 1 (canonical: decoding then re-encoding gives the same string)
```
- A cursor is bound to the endpoint (`kind`) and to the token merchant: a history cursor sent to the audit route, or a
  cursor issued to another merchant, is an InvalidCursorError (→ 422), never a page of anyone's rows. Garbage,
  padding, non-base64url characters, a non-UUID merchant, a version < 1 or above 2147483647 (the int4 column), a
  non-ASCII-decimal / signed / zero-padded version,
  extra separators and non-ASCII bytes are all InvalidCursorError.
- The cursor is not signed: a merchant who crafts a well-formed cursor for itself only chooses where its own history
  starts, which the query scope already bounds.

### 7.2 Page queries — `ops_ai/feedback/thresholds.py`

```python
@dataclass(frozen=True)
class KeysetPage[T]:
    items: list[T]
    next_before: int | None   # the last item's version when more rows exist below it, else None
def rule_history_page(session, scope, limit: int, before: int | None) -> KeysetPage[RuleVersion]
def rule_audit_page(session, scope, limit: int, before: int | None) -> KeysetPage[RuleAuditEntry]
```
- Both: the scope merchant's rows only, `version < before` when `before` is set, `ORDER BY version DESC`, at most
  `limit` items (one extra row is read to decide `next_before`). The items equal the matching slice of
  `rule_history` / `rule_audit`; walking pages until `next_before is None` yields every row exactly once, in order,
  even when new versions are appended between requests (they are newer than any cursor, so they appear only on a
  fresh first page).

### 7.3 Routes (merchant token, `Cache-Control: no-store`, 401/403 unchanged)

Both routes take query `limit` (int 1..200, default 50; 0, 201, non-integer → 422) and `cursor` (1..256 chars,
otherwise 422; an InvalidCursorError → 422 `{"detail": "invalid cursor"}` with `Cache-Control: no-store`).
- `GET /v1/feedback/fraud-rules` → `{active, history, next_cursor?}`: `active` is always the newest version (not the
  page's), `history` is the page.
- `GET /v1/feedback/fraud-rules/audit` → `{entries, next_cursor?}`.
- `next_cursor` is the cursor of the next page or `null` on the last page. Backward compatibility: when the request
  carries neither `limit` nor `cursor` and the first page is also the last, the key is omitted, so the response is
  byte-identical to the unpaged one (`{active, history}` / `{entries}`); a request with `limit` or `cursor` always
  carries `next_cursor`, and a page with more rows below always carries a non-null `next_cursor`.
- Unknown query parameters stay ignored (a `merchant_id` parameter never changes the scope).
