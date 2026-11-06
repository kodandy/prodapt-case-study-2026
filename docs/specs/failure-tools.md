# Failure analysis, tool registry and ops alerts — test specification (11-05)

Source: `docs/requirements-traceability.md` rows B4.1–B4.3, B5.2, B8.5, B9, B11.2 (ops-ai part), SQ1.
`/v1/assistant/investigate` and everything in `docs/specs/sq1.md` stay exactly as they are. Everything below is new.

All new code lives under `services/ops-ai/src/ops_ai/`. Tests go in the `__tests__/` folder next to each module,
follow AAA, use the shared `session_factory` / `redis_client` fixtures from the root `conftest.py`, and never reach
the internet (the OpenAI adapter is tested against the local fake server in `assistant/__tests__/fake_openai.py`).

Common rules:
- "Failed" means `state in ("declined", "failed")`. Every query filters on the scope's merchant (or user).
- The scope always comes from the verified token and is passed in by the caller. No tool argument, LLM output or
  request body field can set or widen it.
- No answer or tool output carries a card number, `card_ref` or `rule_id`.

## 1. Tool-calling port — `assistant/tool_calling.py`

```python
@dataclass(frozen=True) class ToolSpec(name: str, description: str, parameters: Mapping[str, object])  # JSON Schema
@dataclass(frozen=True) class ToolChoiceRequest(instructions: str, question: str, tools: tuple[ToolSpec, ...])
@dataclass(frozen=True) class ToolChoice(name: str, arguments: str)   # raw JSON text from the model: untrusted
@dataclass(frozen=True) class ToolPlan(calls: tuple[ToolChoice, ...], model: str)
class ToolCallingLlm(Protocol):
    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan: ...   # any failure → LlmUnavailableError
```

## 2. OpenAI function calling — `assistant/openai_tool_calling.py`

`OpenAiToolCallingLlm(api_key, chat_model, fallback_model, base_url=OPENAI_BASE_URL, budget_seconds=LLM_BUDGET_SECONDS)`
satisfies `ToolCallingLlm`. Same key/base-URL validation, budget, single fallback attempt on retryable failures
(timeout, transport error, 404/408/409/429/5xx) and error hygiene as `OpenAiLlm` (docs/specs/llm-adapter.md).

- One `POST {base_url}/responses` per attempt. JSON body keys are exactly `model`, `instructions`, `input`, `tools`,
  `tool_choice` (= `"required"`), `max_output_tokens` (= 600), `store` (= `false`).
- `tools` = one `{"type": "function", "name", "description", "parameters", "strict": false}` per `ToolSpec`, in order.
- `input` is the question passed through the same sanitising as `OpenAiLlm` (tags stripped, card numbers masked,
  2000-char cap).
- Success = HTTP 200, JSON object, `status == "completed"`. Every `output` item with `type == "function_call"` and
  string `name` and `arguments` becomes a `ToolChoice(name, arguments)`, in order, at most 8. No such item →
  `ToolPlan(calls=())`. `ToolPlan.model` = the response `model` string (configured model when absent/invalid).
- Anything else → `LlmUnavailableError` whose message never contains the key, the question or the body.

## 3. Grounding — `assistant/grounding.py` (B5.2)

```python
ABSTAIN_TEXT = "Not enough data to answer this from the records available."
@dataclass(frozen=True) class Claim(text: str, evidence_refs: tuple[str, ...])
def ground_claims(claims: Iterable[Claim], evidence: Sequence[EvidenceItem]) -> tuple[Claim, ...]
def cited_refs(text: str) -> tuple[str, ...]
def grounded_narration(text: str, evidence: Sequence[EvidenceItem]) -> str | None
```
- `ground_claims` keeps, in order, only claims with at least one ref where every ref is the `ref` of an evidence item of
  this turn. Duplicate refs inside a claim are collapsed (first order kept).
- `cited_refs` returns every `[...]` token's inner text (non-empty, no nested `[`/`]`), in order, de-duplicated.
- `grounded_narration` returns `text` when it cites at least one ref and every cited ref is a known evidence ref;
  otherwise None (the narration is dropped, never shown).

## 4. Failure query (B4.1) — `investigation/query.py`

```python
FailureMethod = Literal["card", "bank_transfer", "wallet", "upi"]
FailureStatus = Literal["declined", "failed"]
class FailureQueryArgs(BaseModel):          # extra="forbid", frozen
    lookback_hours: int = 24                # 1..720
    end_hours_ago: int = 0                  # 0..720; lookback_hours + end_hours_ago <= 720
    method: FailureMethod | None = None
    status: FailureStatus | None = None     # None = both failed states
    country: str | None = None              # ^[A-Z]{2}$
    currency: str | None = None             # ^[A-Z]{3}$
@dataclass(frozen=True) class FailureQuery(merchant_id: uuid.UUID, start: datetime, end: datetime,
    method: FailureMethod | None, status: FailureStatus | None, country: str | None, currency: str | None,
    source: Literal["llm", "rules"], unapplied_filters: tuple[str, ...])
SUBMIT_FAILURE_QUERY = "submit_failure_query"
def rules_failure_args(question: str) -> FailureQueryArgs
def to_failure_query(args: FailureQueryArgs, scope: Scope, now: datetime, source) -> FailureQuery
def parse_failure_query(question: str, scope: Scope, now: datetime, tool_llm: ToolCallingLlm | None) -> FailureQuery
```
- `to_failure_query`: `end = now - end_hours_ago h`, `start = end - lookback_hours h`, `merchant_id = scope.merchant_id`.
  `unapplied_filters == ("country",)` when `country` is set (payment facts carry no country), else `()`.
- `parse_failure_query`: `tool_llm` None → rules (`source == "rules"`). Otherwise exactly one `choose_tools` call whose
  request offers exactly one tool, `submit_failure_query`, with `parameters == FailureQueryArgs.model_json_schema()`,
  and the bounded question. A plan with exactly one call named `submit_failure_query` whose arguments are a JSON object
  valid for `FailureQueryArgs` → that query, `source == "llm"`. Anything else (LlmUnavailableError, no call, two calls,
  another name, invalid JSON, schema error, an extra key such as `merchant_id`) → rules, `source == "rules"`.
  The merchant is `scope.merchant_id` in every case.
- `rules_failure_args` (lower-cased question, first match per field):
  - window: "this afternoon" / "this morning" / "this evening" → 6 h; "last N hour(s)" / "past N hour(s)" → N h;
    "last N day(s)" / "past N day(s)" → 24·N h; "yesterday" → lookback 24 h, end_hours_ago 24; "this week" /
    "last week" → 168 h; "today" → 24 h; otherwise 24 h. Values above the bounds are clamped to them.
  - method: "bank transfer" → bank_transfer; "wallet" → wallet; "upi" → upi; "card" → card; else None.
  - status: contains "declin" and not "fail" → declined; else None.
  - currency: a whole word USD / EUR / INR / JPY / GBP (any case) → that code.
  - country: "india" → IN, "japan" → JP, "united states" / "usa" → US, "germany" → DE, "france" → FR,
    "united kingdom" / "uk" → GB; else None.

## 5. Failure reasons (B4.2) — `investigation/reasons.py`

```python
@dataclass(frozen=True) class TaxonomyEntry(code: str, category: str, owner: str, retryable: bool)
TAXONOMY: Mapping[str, TaxonomyEntry]
UNCLASSIFIED = "unclassified"
CLASSIFY_FAILURE_REASON = "classify_failure_reason"
MIN_LLM_CONFIDENCE = 0.7
MAX_LLM_CLASSIFICATIONS = 5
def classify_reason(raw: str | None) -> TaxonomyEntry | None          # rules only; None = unmapped
@dataclass(frozen=True) class ReasonLabel(entry: TaxonomyEntry, source: Literal["rules", "llm", "none"],
    confidence: float | None)
def label_reasons(raws: Iterable[str | None], tool_llm: ToolCallingLlm | None) -> dict[str | None, ReasonLabel]
@dataclass(frozen=True) class ReasonGroup(taxonomy_code: str, category: str, owner: str, retryable: bool,
    count: int, share: float, sample_ids: tuple[uuid.UUID, ...], raw_codes: tuple[str, ...], source: str)
@dataclass(frozen=True) class ReasonBreakdown(total_failed: int, groups: tuple[ReasonGroup, ...])
def failure_reason_groups(session, query: FailureQuery, tool_llm) -> ReasonBreakdown
def transfer_reason_groups(session, scope: UserScope, start: datetime, end: datetime, tool_llm) -> ReasonBreakdown
```

| code | category | owner | retryable |
|---|---|---|---|
| issuer_unavailable | issuer | issuer | False |
| do_not_honor | issuer | cardholder | False |
| card_declined | issuer | cardholder | False |
| insufficient_funds | funds | cardholder | False |
| expired_card | card_data | cardholder | False |
| incorrect_cvc | card_data | cardholder | False |
| fraud_flagged | risk | risk_team | False |
| processing_error | processor | acquirer | True |
| rate_limited | processor | platform | True |
| cross_border_limit_kyc_tier | limit | sender | False |
| kyc_verification_failed | compliance | sender | False |
| unclassified | unknown | unknown | False |

Retry policy: `retryable` is True only for `processing_error` and `rate_limited`. Timeouts (issuer_unavailable) and
`unclassified` are never retryable: a timed-out charge has an unknown outcome.

`classify_reason` (rules first): None, empty or whitespace → None. Normalise = lower-case, trim, spaces and hyphens →
`_`. A normalised value equal to a code above (except `unclassified`) → that entry. Otherwise free-text rules on the
lower-cased text, first match wins: "rate limit" / "too many requests" → rate_limited; "issuer unavailable" /
"timeout" / "timed out" → issuer_unavailable; "do not honor" / "do_not_honor" → do_not_honor; "insufficient funds"
/ "insufficient_funds" → insufficient_funds; "expired" → expired_card; "cvc" / "cvv" → incorrect_cvc; "fraud" →
fraud_flagged; "kyc" with "fail" or "verif" → kyc_verification_failed; "limit" with "cross" or "kyc" or "tier" →
cross_border_limit_kyc_tier; "processing error" → processing_error; "declined" → card_declined. Anything else
(including `other`) → None.

`label_reasons`: one label per distinct raw value. Rules hit → `source "rules"`, confidence None. None/empty → the
`unclassified` entry, `source "none"`. An unmapped non-empty value → one `choose_tools` call (offering exactly one tool
`classify_failure_reason` with properties `taxonomy_code` (enum of every code above) and `confidence` (number 0..1),
question = the raw value) for at most MAX_LLM_CLASSIFICATIONS distinct values, in first-seen order; a single call
named `classify_failure_reason` with valid args and `confidence >= 0.7` → that entry, `source "llm"`, its confidence.
Anything else (no LLM, over the cap, LlmUnavailableError, invalid output, low confidence) → `unclassified`,
`source "none"`.

`failure_reason_groups`: failed payment facts of `query.merchant_id` with `query.start <= occurred_at < query.end`,
filtered by `method`, `status` (that state only) and `currency` when set. Each row's raw value is its `decline_code`.
Rows with the same taxonomy code form one group: `count`, `share = count / total_failed`, `sample_ids` = up to 5
newest payment ids (occurred_at desc, payment_id), `raw_codes` = sorted distinct raw values (None left out),
`source` = the label source (`"rules"` wins over `"llm"` over `"none"` when mixed). Groups sorted by count desc then
code. No failures → `ReasonBreakdown(0, ())`.
`transfer_reason_groups`: the same over `transfer_facts` of `scope.user_id` with `status == "failed"` and
`start <= decided_at < end`, raw value `reason_code`, sample ids `transfer_id`.

## 6. Baseline comparison and change timeline (SQ1) — `investigation/tools.py` (additions)

```python
MIN_ELEVATED_FAILURES = 10
@dataclass(frozen=True) class BaselineComparison(window: FailureAggregate, baseline: FailureAggregate,
    ratio: float | None, elevated: bool)
def compare_to_baseline(session, scope, start: datetime, end: datetime, baseline_days: int = 7) -> BaselineComparison
@dataclass(frozen=True) class TimelineBucket(start: datetime, end: datetime, total: int, failed: int, rate: float)
ChangeKind = Literal["failure_onset", "new_decline_code", "bin_onset", "incident_started"]
@dataclass(frozen=True) class ChangeEvent(at: datetime, kind: ChangeKind, subject: str, detail: str)
@dataclass(frozen=True) class ChangeTimeline(buckets: tuple[TimelineBucket, ...], changes: tuple[ChangeEvent, ...])
MAX_TIMELINE_BUCKETS = 96
def change_timeline(session, scope, start, end, bucket_minutes: int = 60, baseline_days: int = 7) -> ChangeTimeline
```
- `compare_to_baseline`: `window = aggregate_failures(start, end)`, `baseline = aggregate_failures(start - days, start)`.
  `ratio = window.rate / baseline.rate`, None when the baseline rate is 0. `elevated` iff `window.failed >= 10` and
  `window.rate >= 2 * baseline.rate` (cross-multiplied, exact).
- `change_timeline`: card payments of the scope. Buckets cover `[start, end)` back to back from `start`, each
  `bucket_minutes` long (the last one ends at `end`, possibly shorter), empty buckets included. More than 96 buckets or
  `start >= end` → ValueError. Changes, sorted by (`at`, `kind`, `subject`):
  - `failure_onset` (at most one): the first bucket with `failed >= 3` and `rate >= 2 * baseline.rate`;
    `at` = bucket start, `subject = "card"`.
  - `new_decline_code`: each decline code with failures in the window and none in the baseline; `at` = its first
    failure in the window; `subject` = the code.
  - `bin_onset`: each BIN with `>= 3` window failures whose window failure rate is `>= 2 *` its baseline failure rate;
    `at` = its first failure in the window; `subject` = the BIN.
  - `incident_started`: incidents visible to the scope (its own or platform-wide) with `start <= started_at < end`;
    `at = started_at`, `subject = incident_id`.
  `detail` is a short human sentence with the numbers.

## 7. Playbook and diagnosis (B4.3) — `investigation/playbook.py`, `investigation/playbooks/failures.v1.json`, `investigation/diagnosis.py`

```python
@dataclass(frozen=True) class PlaybookEntry(taxonomy_code: str, fix: str, hypothesis: str, how_to_confirm: tuple[str, ...])
@dataclass(frozen=True) class Playbook(version: str, entries: Mapping[str, PlaybookEntry])
DEFAULT_PLAYBOOK_PATH: Path
def load_playbook(path: Path = DEFAULT_PLAYBOOK_PATH) -> Playbook
```
File format `{"version": str, "entries": [{"taxonomy_code", "fix", "hypothesis", "how_to_confirm": [str, ...]}]}`.
`load_playbook` raises ValueError when the file is malformed, a code is unknown or duplicated, or any TAXONOMY code
lacks an entry. The shipped file covers every code and has a non-empty version.

```python
SIGNIFICANT_SHARE = 0.1
@dataclass(frozen=True) class RankedFix(rank: int, taxonomy_code: str, fix: str, playbook_version: str,
    evidence_refs: tuple[str, ...], incident_refs: tuple[str, ...])
@dataclass(frozen=True) class Diagnosis(facts: tuple[Claim, ...], hypotheses: tuple[Claim, ...],
    how_to_confirm: tuple[Claim, ...], fixes: tuple[RankedFix, ...])
def diagnose(comparison, timeline, breakdown, incidents: Sequence[IncidentHit], playbook) -> Diagnosis
```
Evidence refs used (produced by section 8): `aggregate:window`, `aggregate:baseline`, `comparison:baseline`,
`aggregate:bin:<bin>`, `change:<kind>:<subject>`, `reason:<taxonomy_code>`, `incident:<id>`.
- `facts` (always): window rate claim [`aggregate:window`]; baseline rate claim [`aggregate:baseline`]; comparison
  claim [`comparison:baseline`]; when the window has failures with a top BIN, its share of failures
  [`aggregate:bin:<bin>`]; each change event [`change:<kind>:<subject>`]; each reason group with
  `share >= 0.1`: "<pct> of failures are <code> (owner <owner>, retryable yes/no)" [`reason:<code>`].
- When `comparison.elevated` is False: `hypotheses`, `how_to_confirm` and `fixes` are empty, and `facts` includes a
  claim that the failure rate is within the baseline [`comparison:baseline`].
- When elevated, for each significant group (share >= 0.1) in group order: one hypothesis (playbook text)
  [`reason:<code>`, plus `aggregate:bin:<top bin>` when the top BIN holds >= 50 % of window failures]; one
  how-to-confirm claim per playbook step [`reason:<code>`]; one `RankedFix` (rank 1, 2, … in group order) with the
  playbook fix, `playbook_version`, `evidence_refs == ("reason:<code>",)`, and `incident_refs` = `incident:<id>` of
  each incident whose `root_cause` or `title` classifies (classify_reason) to the same code. When such an incident
  exists its `fix_applied` is appended to the fix text as ` Previously applied: <fix_applied>`.

## 8. Failure investigation v2 (B4 + SQ1) — `investigation/failures.py`

```python
@dataclass(frozen=True) class FailureAnswer(query: FailureQuery, comparison: BaselineComparison,
    timeline: ChangeTimeline, breakdown: ReasonBreakdown, diagnosis: Diagnosis, claims: tuple[Claim, ...],
    abstained: bool, tool_calls: tuple[ToolCall, ...], evidence: tuple[EvidenceItem, ...], mode: AnswerMode,
    text: str | None, model: str | None)
def investigate_failures(session, scope, question, now, llm: LlmPort, tool_llm: ToolCallingLlm | None,
    playbook: Playbook) -> FailureAnswer
```
1. `query = parse_failure_query(question[:2000], scope, now, tool_llm)`.
2. Tool calls, in this order, recorded with string args: `compare_baseline` {start, end, baseline_days="7"},
   `change_timeline` {start, end, bucket_minutes}, `failure_reasons` {start, end + each set filter},
   `search_incidents` {query}, `list_failed_payments` {start, end, limit="5"}. `bucket_minutes` = 60 when the window
   is <= 24 h, else the smallest of 120, 360, 1440 that keeps <= 96 buckets. Incident query =
   `"BIN <top bin> <top code words>"` when a top BIN exists, else `"<top code words> card failures"`, where top code words
   is the top group's code with `_` → space (no groups → "card failures").
3. Evidence, in order: `aggregate:window`, `aggregate:baseline`, `comparison:baseline`, `aggregate:bin:<top bin>`
   (when any), `change:<kind>:<subject>` per change, `reason:<code>` per group (summary has count, share, owner,
   retryable, raw codes), `incident:<id>` per hit, `payment:<id>` for the 5 newest failed payments matching the query.
4. `diagnosis = diagnose(...)`; `claims = ground_claims(facts + hypotheses + how_to_confirm + one claim per fix
   "Fix <rank>: <fix>" with the fix's evidence and incident refs)`.
5. Abstain when `comparison.window.total == 0` or no claim survives grounding: `abstained == True`, `claims == ()`,
   `text == ABSTAIN_TEXT`, `mode == "t2_fallback"`, `model` None, and the LLM is not called.
6. Otherwise one `llm.complete(LlmRequest(question, evidence))`. LlmUnavailableError → `mode "t2_fallback"`, text None.
   A narration that fails `grounded_narration` → `mode "t2_fallback"`, text None. Else `mode "llm"`.
The session is closed before the LLM call.

## 9. Tool registry (B8.5, B9) — `tools/registry.py`, `tools/catalog.py`, `tools/planner.py`

```python
class ToolRejectedError(Exception): reason: Literal["unknown_tool", "invalid_args"]
class ToolExecutionError(Exception): tool: str
@dataclass(frozen=True) class ToolOutput(evidence: tuple[EvidenceItem, ...], claims: tuple[Claim, ...])
@dataclass(frozen=True) class ToolContext(Generic[S]): session: Session; scope: S; now: datetime
@dataclass(frozen=True) class ToolDefinition(Generic[S]):
    name: str; description: str; args_model: type[BaseModel]; run: Callable[[ToolContext[S], BaseModel], ToolOutput]
class ToolRegistry(Generic[S]):
    def __init__(self, tools: Sequence[ToolDefinition[S]]) -> None    # duplicate name → ValueError
    def names(self) -> tuple[str, ...]
    def specs(self) -> tuple[ToolSpec, ...]                          # parameters = args_model.model_json_schema()
    def validate(self, name: str, arguments: str) -> BaseModel
    def invoke(self, session: Session, scope: S, now: datetime, name: str, arguments: str) -> ToolOutput
def merchant_tools() -> ToolRegistry[Scope]
def user_tools() -> ToolRegistry[UserScope]
```
- `validate`: unknown name → `ToolRejectedError("unknown_tool")`. Arguments longer than 4096 chars, not JSON, not a
  JSON object, or invalid for `args_model` → `ToolRejectedError("invalid_args")`. Every args model forbids extra
  keys, so `merchant_id`, `user_id` or `scope` in the arguments is `invalid_args`. Error messages never contain the
  argument text.
- `invoke` = `validate`, then runs the tool inside a transaction opened with `SET TRANSACTION READ ONLY` that is
  always rolled back. A tool that tries to write fails with `ToolExecutionError` and nothing is written. Any database
  error → `ToolExecutionError(tool=name)`. The session must have no transaction in progress (ValueError otherwise).
- `merchant_tools().names() == ("get_payment_status", "list_failed_payments", "aggregate_failures", "compare_baseline",
  "change_timeline", "failure_reasons", "search_incidents", "analyze_fraud")`. Args (all `extra="forbid"`):
  - `get_payment_status` {payment_id: UUID} → the scope's payment fact: evidence `payment:<id>` (state, decline
    code, amount, currency, time) and one claim citing it. Another merchant's or an unknown id → empty output.
  - `list_failed_payments` FailureQueryArgs fields + `limit` 1..50 (default 20) → evidence
    `aggregate:failed_payments` (count) + `payment:<id>` newest first; claim citing `aggregate:failed_payments`.
  - `aggregate_failures` {lookback_hours 1..720 = 6, end_hours_ago 0..720 = 0} → `aggregate:window` + claim.
  - `compare_baseline` {window_hours 1..72 = 6, baseline_days 1..30 = 7} → `aggregate:window`,
    `aggregate:baseline`, `comparison:baseline` + one claim citing all three.
  - `change_timeline` {window_hours 1..72 = 6, bucket_minutes one of 15/30/60 = 60, baseline_days 1..30 = 7} →
    `change:<kind>:<subject>` per change (claim each), plus `timeline:<bucket start ISO>` per bucket with failures.
  - `failure_reasons` FailureQueryArgs → `reason:<code>` per group + claim each (the tool passes no LLM).
  - `search_incidents` {query: 1..200 chars} → `incident:<id>` + claim each.
  - `analyze_fraud` {lookback_hours 1..168 = 24} → `episode:<first_payment_id>` per flagged episode + claim each.
- `user_tools().names() == ("get_payment_status", "failure_reasons")`:
  - `get_payment_status` {transfer_id: UUID | None = None} → the scope's transfer (`transfers.tools.get_payment_status`),
    evidence `transfer:<id>` with the sender-facing status (never `rule_id`, never `flagged`), one claim.
  - `failure_reasons` {lookback_hours 1..720 = 720} → `transfer_reason_groups` → `reason:<code>` + claims.
- `RuleBasedToolPlanner()` satisfies `ToolCallingLlm`, `model == "rules"`. It picks only among offered tools, each at
  most once, in this fixed order: `get_payment_status` when the question holds a UUID (argument `payment_id` or
  `transfer_id`, whichever the offered schema has) or, for a schema without a required id, when the question says
  "status" or "transfer"; `analyze_fraud` {} on "fraud" / "card testing" / "micro"; `compare_baseline` {},
  `change_timeline` {} and `failure_reasons` (rules_failure_args JSON, only the keys the offered schema has) on
  "fail" / "declin"; `search_incidents` {"query": first 200 chars} on "incident" / "outage" / "fix".

## 10. Tool-calling assistant (B9, B5.2) — `assistant/tool_assistant.py`

```python
MAX_TOOL_CALLS = 4
@dataclass(frozen=True) class RejectedCall(name: str, reason: Literal["unknown_tool", "invalid_args",
    "limit_exceeded", "execution_failed"])
@dataclass(frozen=True) class ToolAnswer(claims, abstained: bool, tool_calls: tuple[ToolCall, ...],
    rejected_calls: tuple[RejectedCall, ...], evidence, mode, text, model, planner_model: str)
def answer_with_tools(session_factory, scope: S, question: str, now: datetime, registry: ToolRegistry[S],
    planner: ToolCallingLlm, llm: LlmPort) -> ToolAnswer
```
- One `planner.choose_tools(ToolChoiceRequest(fixed instructions, question[:2000], registry.specs()))`.
  LlmUnavailableError → `RuleBasedToolPlanner` instead (`planner_model == "rules"`).
- Calls run in plan order through `registry.invoke`, each in its own fresh session. The 5th and later →
  `limit_exceeded`. Unknown/invalid → rejected with that reason and not run. `ToolExecutionError` →
  `execution_failed`. Accepted calls are recorded as `ToolCall(name, {key: str(value)})` from the validated args
  (JSON mode, None values left out). The scope passed to every tool is the caller's.
- `claims = ground_claims(all tool claims, all tool evidence)`. No claim → abstain exactly as in section 8 step 5.
  Otherwise one `llm.complete` over the evidence; narration kept only if `grounded_narration` accepts it.

## 11. Routes — `assistant/routes.py` (mounted by `create_app`)

`OpsAiAppDeps` gains `tool_llm: ToolCallingLlm | None = None` and `playbook: Playbook` (default `load_playbook()`).
- `POST /v1/assistant/failures` (merchant token, or ops token with `?merchant_id=`; body `{question}` with extra
  fields → 422) → 200 with
  `Cache-Control: no-store` and keys exactly `query`, `abstained`, `mode`, `text`, `model`, `comparison`, `timeline`,
  `reasons`, `facts`, `hypotheses`, `how_to_confirm`, `fixes`, `claims`, `tool_calls`, `evidence`.
  `query` = {start, end, method, status, country, currency, source, unapplied_filters} (no merchant id);
  `reasons` items = {taxonomy_code, category, owner, retryable, count, share, sample_ids, source};
  claims = {text, evidence_refs}; fixes = {rank, taxonomy_code, fix, playbook_version, evidence_refs, incident_refs}.
- `POST /v1/assistant/query` (merchant token → `merchant_tools()`, user token → `user_tools()`; same body) → keys
  exactly `abstained`, `mode`, `text`, `model`, `planner_model`, `claims`, `tool_calls`, `rejected_calls`,
  `evidence`; `Cache-Control: no-store`. Planner = `deps.tool_llm` or `RuleBasedToolPlanner()`.
- Missing/invalid token → 401 `{"detail": "invalid token"}`; a user token on `/failures` → 403.
- Principals on `/failures` (dependency `failure_scope` in `assistant/routes.py`; authentication runs first, so
  401 and 403 always come before a 422):
  - merchant token (`merchant_admin` or `merchant_support`): unchanged, the merchant comes from the token only. A
    `merchant_id` query parameter (any value, the token's own merchant included) → 422
    `{"detail": "merchant_id is taken from the token"}` with `Cache-Control: no-store`, before any work.
  - ops token (`ops_agent` or `ops_lead`, the ops-ai token issued at ops sign-in by `POST /v1/ops/ops-ai-tokens`;
    checked by `scope.require_ops_role`): must name exactly one merchant with `?merchant_id=<uuid>`. Missing,
    empty, not a canonical lower-case hyphenated UUID (braces, `urn:uuid:`, bare hex and upper case are rejected, as for the token claim), repeated, or any implicit "all" → 422 `{"detail": "merchant_id is required for ops staff"}`
    with `Cache-Control: no-store`, before any work. There is no all-merchants failure query: the investigation
    is per merchant, like `/v1/ops/analytics/forecast` and `/chargeback-risk` (docs/specs/analytics.md section 4).
    The named merchant becomes `Scope(merchant_id=...)` and the response equals what that merchant's own token
    gets (same keys, same masking: no merchant id in `query`, no `raw_codes` in `reasons`). The body stays
    `{question}`; `merchant_id` in the body is 422 for every principal.
  - any other principal (user token) → 403 `{"detail": "forbidden"}`; an ops token on `/query` stays 403.
  - Logging: ops reads on `/failures` record nothing beyond the request metrics and traces by route template that
    every route has, the same as the other ops-ai ops reads; tokens are never logged. The audit gap is the one
    listed in docs/specs/analytics.md section 4 "Known residuals".
- `main.py`: with OPENAI_API_KEY set, `tool_llm = OpenAiToolCallingLlm(...)` with the same model settings; else None.

## 12. Ops alerts (B11.2, ops-ai side) — `alerts/`

Tables (Alembic migration on top of `b12c4e7a9f31`):
- `ops_ai.ops_alerts`: `alert_id` UUID PK, `source_key` Text unique, `kind` String(32), `severity`
  String(16) (`high` / `medium` / `low`), `merchant_id` UUID indexed, `fraud_alert_id` UUID nullable, `title` Text,
  `body` Text, `created_at`, `acknowledged_at` nullable, `acknowledged_by` UUID nullable.
- `ops_ai.ops_alert_deliveries`: `delivery_id` UUID PK, `alert_id` FK → ops_alerts, `channel` String(32),
  `status` String(16) (`pending` / `delivered` / `failed`, check constraint), `attempts` int, `stream_entry_id`
  nullable, `last_error` String(64) nullable, `created_at`, `delivered_at` nullable; unique (`alert_id`, `channel`).

```python
OPS_ALERTS_STREAM = "ops.alerts.v1"
OPS_ALERT_CHANNEL = "redis_stream"
MAX_DELIVERY_ATTEMPTS = 5
def raise_ops_alert(session, clock, *, source_key: str, kind: str, severity: str, merchant_id: uuid.UUID,
    fraud_alert_id: uuid.UUID | None, title: str, body: str) -> uuid.UUID | None
def deliver_pending(session_factory, redis, clock, limit: int = 100) -> int
def list_ops_alerts(session, *, status: Literal["open", "acknowledged", "all"], limit: int) -> list[OpsAlertView]
def acknowledge_ops_alert(session, clock, alert_id: uuid.UUID, staff_id: uuid.UUID) -> OpsAlertView | None
def create_alerts_router(session_factory, clock, staff_id: Callable[..., uuid.UUID]) -> APIRouter
```
- `raise_ops_alert` inserts the alert and one `pending` delivery for `redis_stream`; a repeated `source_key` inserts
  nothing and returns None. Does not commit.
- `fraud_watch.watch_payment`: when it first inserts a fraud alert it also calls `raise_ops_alert` with
  `source_key = window_key`, `kind "card_testing"`, `severity "high"`, the fraud alert id, title
  "Card testing suspected", body = the ticket reason. A refresh or a redelivered event raises no second ops alert.
  Alert body and stream fields never contain payment ids, card refs or BIN-plus-last4.
- `deliver_pending`: takes up to `limit` pending deliveries (oldest first, `FOR UPDATE SKIP LOCKED`), XADDs one entry
  per delivery to `ops.alerts.v1` with fields `alert_id`, `kind`, `severity`, `merchant_id`, `title`, `created_at`
  (approximate maxlen 10,000), and marks it `delivered` with `stream_entry_id` and `delivered_at`. A Redis error leaves
  it `pending` with `attempts + 1` and `last_error` = the error class name; at MAX_DELIVERY_ATTEMPTS it becomes
  `failed`. Returns the number delivered. A delivered alert is never sent again.
- `python -m ops_ai.alerts` runs `deliver_pending` in a loop (1 s idle sleep); `--once` runs one pass.
- Router (dependency `staff_id` resolves the ops staff member from the token; merchant and user tokens never reach
  these routes): `GET /v1/ops/alerts?status=open|acknowledged|all&limit=1..200` (default open, 50), newest first;
  `GET /v1/ops/alerts/{alert_id}` (404 unknown); `POST /v1/ops/alerts/{alert_id}/ack` (idempotent: a second ack keeps
  the first `acknowledged_at` / `acknowledged_by`; 404 unknown). Items: `alert_id`, `kind`, `severity`,
  `merchant_id`, `fraud_alert_id`, `title`, `body`, `created_at`, `acknowledged_at`, `acknowledged_by`,
  `delivery_status`. `Cache-Control: no-store`.
