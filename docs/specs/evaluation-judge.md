# Spec: evaluation framework (R2-3), LLM-as-judge (R2-2), eval gate (P0-15)

Status: implemented on `worker/ai-eval-judge`. Code: `services/ops-ai/src/ops_ai/evaluation/`, `services/ops-ai/src/ops_ai/judge/`.
Data: `services/ops-ai/eval/`. Tests: `__tests__/` next to each module.

## 0. Decision: in-repo harness instead of DeepEval

`deepeval` was reviewed (dependency-auditor, 2026-11-05) and rejected: 100–150 transitive packages
(openai, grpcio, opentelemetry-sdk, sentry-sdk, posthog, langchain, a full pytest plugin stack), telemetry on
by default, weak typing under mypy strict, and every response-quality metric needs a judge LLM anyway. The
offline metrics it ships (exact match, tool correctness) are simple to write. So the harness here follows
DeepEval's model (test case → metrics with thresholds → report, run through pytest) with no new dependency.
LLM calls go through the existing `LlmPort`; the real model is `OpenAiLlm` only when `OPENAI_API_KEY` is set.

## 1. Metric results (`ops_ai.evaluation.metrics`)

```python
MetricStatus = Literal["passed", "failed", "skipped", "not_applicable"]

@dataclass(frozen=True)
class MetricResult:
    name: str
    score: float | None        # None when skipped / not_applicable
    threshold: float
    status: MetricStatus
    detail: str = ""

def gate(name: str, score: float, threshold: float, detail: str = "") -> MetricResult
    # passed iff score >= threshold, else failed
def skipped(name: str, threshold: float, detail: str) -> MetricResult
def not_applicable(name: str, threshold: float, detail: str) -> MetricResult
```

Only `failed` fails a gate; `skipped` and `not_applicable` never do.

### 1.1 Deterministic response-quality metrics

```python
def cited_refs(text: str) -> tuple[str, ...]
    # every `[ref]` in order of appearance; ref = one or more chars that are not whitespace, `[` or `]`
def faithfulness(text: str | None, evidence: Sequence[EvidenceItem], question: str = "") -> float | None
def evidence_ref_coverage(refs: Sequence[str], expected_prefixes: Sequence[str]) -> float
def answer_relevancy(observed: Mapping[str, object], expected: Mapping[str, object],
                     text: str, required_terms: Sequence[str]) -> float
```

- `faithfulness`: `None` when `text` is None (T2 fallback has no narration). Claims are (a) each cited ref and
  (b) each number token in the text with the citations removed. Number token regex: `\d+(?:\.\d+)?`.
  A cited ref is supported iff it equals some `evidence[i].ref`. A number is supported iff it is one of the
  number tokens of any evidence summary, any evidence ref, or the question. Score = supported / claims;
  no claims → 1.0.
- `evidence_ref_coverage`: fraction of `expected_prefixes` that prefix at least one ref; no prefixes → 1.0.
- `answer_relevancy`: checks = one per `expected` key (passes iff `observed.get(key) == value`) plus one per
  required term (passes iff the term occurs case-insensitively in `text`). Score = passed / checks; no
  checks → 1.0.

### 1.2 Classification reports (`ops_ai.evaluation.classification`)

```python
@dataclass(frozen=True)
class BinaryReport:
    true_positive: int; false_positive: int; false_negative: int; true_negative: int
    precision: float; recall: float; f1: float; accuracy: float

def binary_report(predicted: AbstractSet[T], actual: AbstractSet[T], universe: AbstractSet[T]) -> BinaryReport
```
`predicted` or `actual` not a subset of `universe` → `ValueError`; empty universe → `ValueError`.
precision = tp/(tp+fp), and when tp+fp = 0 it is 1.0 if fn = 0 else 0.0. recall = tp/(tp+fn), and 1.0 when
tp+fn = 0. f1 = 2pr/(p+r), 0.0 when p+r = 0. accuracy = (tp+tn)/|universe|.

```python
@dataclass(frozen=True)
class ClassScore:
    precision: float; recall: float; f1: float; support: int   # support = expected count

@dataclass(frozen=True)
class MulticlassReport:
    total: int; correct: int; accuracy: float; macro_f1: float
    per_class: Mapping[str, ClassScore]           # keys: every label in expected ∪ predicted
    confusion: Mapping[tuple[str, str], int]      # (expected, predicted) → count, only non-zero cells

def multiclass_report(pairs: Sequence[tuple[str, str]]) -> MulticlassReport   # (expected, predicted)
```
Empty `pairs` → `ValueError`. Per class: precision/recall with the zero-division rule above per class
(precision 0.0 when the class was never predicted but has support; 1.0 when neither). macro_f1 = mean f1
over `per_class`.

## 2. Custom metrics

### 2.1 Fraud prediction accuracy (`ops_ai.evaluation.fraud`)

```python
def card_testing_labels(log: ScenarioLog) -> frozenset[uuid.UUID]
    # transaction ids of merchant card payments with scenario == "sq3_card_testing"
def seeded_card_payment_ids(log: ScenarioLog, anchor: datetime) -> frozenset[uuid.UUID]
    # ids of transactions that ops_ai.seed.to_payment_event maps to an event and whose shifted time <= anchor
def fraud_prediction_report(session_factory: sessionmaker[Session], log: ScenarioLog,
                            anchor: datetime) -> BinaryReport
```
`fraud_prediction_report` assumes `seed_dataset(session_factory, clock, log, anchor)` already ran. For every
merchant in the log it calls `analyze_fraud(session, Scope(merchant_uuid(m)), start, anchor + 1 s)` with
`start` = earliest shifted transaction time; predicted = the payment ids of every episode with
`flagged=True`; actual = `card_testing_labels ∩ universe`; universe = `seeded_card_payment_ids`.
Gate thresholds: precision ≥ 0.95, recall ≥ 0.95, f1 ≥ 0.95.

### 2.2 Payment failure prediction (`ops_ai.evaluation.failure`)

Label file `services/ops-ai/eval/failure_labels.json`:
```json
{"version": 1,
 "reason_catalog": {"<exact failure_reason in the generator>": "<expected decline code>", ...},
 "cases": [{"text": "<free-text failure reason>", "expected": "<decline code>"}, ...]}
```
```python
@dataclass(frozen=True)
class LabeledReason: text: str; expected: str
@dataclass(frozen=True)
class FailureLabelSet: reason_catalog: Mapping[str, str]; cases: tuple[LabeledReason, ...]

def load_failure_labels(path: Path) -> FailureLabelSet       # bad shape / unknown version → ValueError
def dataset_failure_cases(log: ScenarioLog, catalog: Mapping[str, str]) -> tuple[LabeledReason, ...]
    # merchant card payments with status failed|flagged; expected = catalog[failure_reason];
    # a reason missing from the catalog → ValueError naming it
def failure_prediction_report(cases: Sequence[LabeledReason],
                              classify: Callable[[str], str] = failure_code) -> MulticlassReport
```
Gates: dataset accuracy ≥ 0.99 and macro-F1 ≥ 0.99; paraphrase set (`cases`) accuracy ≥ the threshold in
§5 (the rules-first classifier misses some paraphrases; the number is reported honestly).

## 3. Cross-tenant leakage gate (`ops_ai.evaluation.leakage`)

```python
@dataclass(frozen=True)
class Leak: path: str; token: str     # path like "$.evidence[3].ref", "$.alerts[0].alert_id", "$.<key>"

def find_leaks(payload: object, foreign: AbstractSet[str]) -> tuple[Leak, ...]
def leakage_metric(leaks: Sequence[Leak]) -> MetricResult
```
`find_leaks` walks dicts (keys and values), lists, strings, numbers, bools and None. Each string (including
keys) is split into tokens with `[A-Za-z0-9_-]+`; a token leaks when it, or its lowercase form, is in
`foreign`. Results in walk order (dict insertion order). `leakage_metric`: name `cross_tenant_leakage`,
threshold 1.0, score 1.0 with no leaks else 0.0 (a single leak fails); `detail` names the count and the paths,
never the tokens.

## 4. Golden cases (`ops_ai.evaluation.golden`)

One JSON file per case under `services/ops-ai/eval/golden/`:
```json
{"case_id": "sq1_bin_outage",
 "sample_query": "SQ1",
 "route": "/v1/assistant/investigate",
 "principal": "sq1_merchant",
 "body": {"question": "..."},
 "expect": {"status": 200,
            "fields": {"intent": "failure_spike", "cause_label": "issuer_outage_on_bin"},
            "evidence_ref_prefixes": ["aggregate:window", "aggregate:bin:400000", "incident:"],
            "required_terms": ["400000"],
            "tool_calls": ["aggregate_failures", "compare_baseline", "search_incidents"]},
 "thresholds": {"faithfulness": 0.9, "answer_relevancy": 1.0, "evidence_ref_coverage": 1.0},
 "hypotheses": {"items": [Hypothesis, ...], "expected_order": ["h_bin", "h_acquirer", "h_integration"]}}
```
- `principal`: `sq1_merchant | sq3_merchant | sq4_merchant | sq2_user | quiet_merchant` (resolved by the
  runner from the planted scenarios; `quiet_merchant` = `mch_0010`).
- `fields`: dotted paths into the response JSON (`"transfer.status"`), compared with `==`.
- `required_terms`: searched in the response's `text`, `summary` and `suggested_fixes` joined.
- `tool_calls`: expected `tool_calls[].name` in order (part of answer relevancy as one check).
- `hypotheses` optional: ranked with `rank_hypotheses` against the response evidence (§6.2).

```python
class GoldenCase(BaseModel)   # pydantic, extra="forbid", frozen
def load_golden_cases(directory: Path) -> tuple[GoldenCase, ...]
    # every *.json, sorted by case_id; duplicate case_id or invalid file → ValueError naming the file

@dataclass(frozen=True)
class CaseResult:
    case_id: str; sample_query: str; metrics: tuple[MetricResult, ...]
    @property
    def passed(self) -> bool    # no metric failed

def score_case(case: GoldenCase, status_code: int, body: Mapping[str, object],
               foreign: AbstractSet[str], judge_llm: LlmPort | None = None) -> CaseResult
```
`score_case` metrics in order: `status` (1.0/0.0, threshold 1.0), `faithfulness` (not_applicable when the
response `text` is null), `answer_relevancy`, `evidence_ref_coverage`, `cross_tenant_leakage`, then
`hypothesis_ranking` (only with `hypotheses`: 1.0 iff the ranked ids equal `expected_order`, threshold 1.0),
then `llm_faithfulness` and `llm_answer_relevancy` (§4.1). Missing threshold keys default to 1.0 (0.9 for
faithfulness).

### 4.1 LLM-judged metrics (`ops_ai.evaluation.llm_metrics`)

```python
def llm_faithfulness(llm: LlmPort | None, question: str, text: str | None,
                     evidence: Sequence[EvidenceItem], threshold: float = 0.7) -> MetricResult
def llm_answer_relevancy(llm: LlmPort | None, question: str, text: str | None,
                         threshold: float = 0.7) -> MetricResult
def judge_llm_from_env(environ: Mapping[str, str] = os.environ) -> LlmPort | None
```
`llm` None → `skipped` ("no OPENAI_API_KEY"). `text` None → `not_applicable`. Otherwise one `LlmPort.complete`
call whose question holds the rubric, the answer and the original question, with the evidence as evidence;
the reply must be JSON `{"score": 0..1, "reason": str ≤ 300}` (fences allowed). Schema-invalid reply →
`failed` with detail "judge reply rejected". `LlmUnavailableError` → `skipped` ("judge unavailable").
`judge_llm_from_env`: `OpenAiLlm` when `OPENAI_API_KEY` is non-empty (model `OPENAI_CHAT_MODEL` or the
default), else None. The key is never logged or put in a detail.

## 5. Suite and report (`ops_ai.evaluation.suite`, `ops_ai.evaluation.report`)

```python
@dataclass(frozen=True)
class EvalReport:
    cases: tuple[CaseResult, ...]
    suite_metrics: tuple[MetricResult, ...]
    @property
    def passed(self) -> bool
    def failures(self) -> tuple[str, ...]      # "<case_id>:<metric>" / "suite:<metric>" for each failed

def report_to_json(report: EvalReport) -> str          # stable keys, sorted cases
def report_from_json(text: str) -> EvalReport
def render_text(report: EvalReport) -> str             # one row per metric: scope, name, score, threshold, status

def run_full_eval(session_factory: sessionmaker[Session], anchor: datetime,
                  answer_llm: LlmPort, judge_llm: LlmPort | None) -> EvalReport
```
`run_full_eval` generates the dataset (seed 42, 12 000 records), seeds payments, transfers and settlement at
`anchor`, builds the real app with a throwaway RSA key and `answer_llm`, mints a token per principal, posts
every golden case through the HTTP API (FastAPI TestClient) and scores it. The foreign set of a merchant
principal = every other merchant's uuid, the ids of their seeded payments and transfers, and the ids of
incidents scoped to another merchant; of the user principal = the ids of every transaction whose `user_id`
is another user. Suite metrics: `fraud_precision`, `fraud_recall`, `fraud_f1`, `failure_dataset_accuracy`,
`failure_dataset_macro_f1`, `failure_paraphrase_accuracy` (threshold 0.75), `judge_dispute_agreement`,
`judge_ranking_agreement` (§6.3).

CLI: `python -m ops_ai.evaluation show <report.json>` prints `render_text` and exits 0 when passed, 1 otherwise.

## 6. LLM-as-judge (`ops_ai.judge`)

Deterministic rubric first, LLM second; the LLM can only add reasons, escalate, or break ties. Its reply is
schema-validated (pydantic, `extra="forbid"`, ≤ 8 KiB, fences allowed); an invalid reply is dropped.

### 6.1 Dispute workflow validation (`ops_ai.judge.dispute`)

Input (`ops_ai.judge.schemas`, pydantic `extra="forbid"`, frozen):
```python
DisputeState = Literal["opened", "under_review", "won", "lost"]
ActorRole = Literal["card_network", "merchant", "ops_lead"]
class DisputeTransition: from_state: DisputeState | None; to_state: DisputeState; actor_role: ActorRole; at: datetime
class DisputeEvidence: evidence_id: str; kind: str; submitted_at: datetime
class DisputeWorkflowRecord:
    dispute_id: uuid.UUID; payment_id: uuid.UUID; merchant_id: uuid.UUID
    network_reason_code: str; currency: str
    payment_amount_minor: int (>0); refunded_minor: int (>=0); dispute_amount_minor: int
    transitions: tuple[DisputeTransition, ...] (min 1)
    evidence: tuple[DisputeEvidence, ...]; evidence_considered: tuple[str, ...]
    outcome: Literal["won", "lost"] | None
    chargeback_minor: int (>=0); chargeback_fee_minor: int (>=0)
```
Legal transitions (mirror `gateway/payments/disputes.py`): `None → opened` by `card_network`;
`opened → under_review` by `merchant`; `under_review → won|lost` by `card_network` or `ops_lead`.

Reason catalog `REASON_CODES: Mapping[str, ReasonCode]` (`category`, `compelling_evidence: frozenset[str]`):
`10.4` fraud card-absent {avs_cvv_match, 3ds_authentication, prior_undisputed_history, device_ip_match};
`12.6.1` duplicate processing {distinct_transactions_proof, refund_issued}; `13.1` not received
{proof_of_delivery, tracking_confirmation}; `13.2` cancelled recurring {cancellation_policy_accepted,
usage_after_cancellation}; `13.3` not as described {product_description, return_policy_accepted};
`13.6` credit not processed {refund_issued, return_policy_accepted}.

`rubric_checks(record) -> tuple[Check, ...]`, `Check(check: str, passed: bool, severity: "hard"|"soft",
reason: str)`, always these checks in this order:
1. `transition_sequence` (hard): first is `None → opened`; each `from_state` equals the previous
   `to_state`; every pair legal.
2. `transition_actors` (hard): each transition by an allowed actor.
3. `transition_order` (hard): `at` non-decreasing.
4. `outcome_matches_state` (hard): last `to_state` is won/lost ⇔ `outcome` equals it; otherwise `outcome` None.
5. `evidence_considered` (hard): when resolved, `set(evidence_considered) == {e.evidence_id}`; when not
   resolved, `evidence_considered` ⊆ submitted ids.
6. `evidence_timing` (hard): every evidence `submitted_at` ≥ open time and ≤ resolution time (when resolved).
7. `dispute_amount` (hard): `0 < dispute_amount_minor ≤ payment_amount_minor − refunded_minor`.
8. `chargeback_amounts` (hard): lost → `chargeback_minor == dispute_amount_minor` and fee > 0; otherwise both 0.
9. `reason_code_known` (soft): code in `REASON_CODES`.
10. `outcome_supported_by_evidence` (soft): won → at least one considered evidence kind is compelling for the
    code; lost → no considered evidence kind is compelling; unresolved or unknown code → passed.

`rubric_verdict`: `invalid` if any hard check failed, else `needs_review` if any soft failed, else `valid`.

```python
class JudgeReason: code: Literal[<check names> | "llm_concern"]; detail: str (≤300)
class DisputeVerdict:
    dispute_id: uuid.UUID; verdict: Verdict; rubric_verdict: Verdict; checks: tuple[Check, ...]
    reasons: tuple[JudgeReason, ...]       # one per failed check (rubric) then LLM reasons
    llm_status: Literal["skipped", "used", "rejected", "unavailable"]; model: str | None

def judge_dispute(record: DisputeWorkflowRecord, llm: LlmPort | None = None) -> DisputeVerdict
```
LLM reply schema: `{"verdict": "valid"|"needs_review"|"invalid", "reasons": [{"code", "detail"}] (≤10)}`.
Final verdict = rubric verdict, raised to `needs_review` when the rubric says `valid` and the LLM says
`needs_review` or `invalid`. The LLM never lowers a verdict and never makes it `invalid`.

### 6.2 Root-cause hypothesis ranking (`ops_ai.judge.hypotheses`)

```python
class Hypothesis:  hypothesis_id: str (pattern [A-Za-z0-9_-]{1,64}); cause_code: str (≤64);
                   statement: str (≤500); evidence_refs: tuple[str, ...] (≤20)
class RankedHypothesis:
    rank: int (1-based); hypothesis_id; cause_code; statement
    support_score: float; supported: bool
    valid_refs: tuple[str, ...]; fabricated_refs: tuple[str, ...]
    resolution_steps: tuple[str, ...]; llm_rationale: str | None
class HypothesisRanking:
    ranked: tuple[RankedHypothesis, ...]; llm_status: same literal as disputes; model: str | None

def ref_weight(ref: str) -> float
def rank_hypotheses(hypotheses: Sequence[Hypothesis], evidence: Sequence[EvidenceItem],
                    llm: LlmPort | None = None) -> HypothesisRanking
```
- `ref_weight` by prefix: `aggregate:bin:` 3.0, `aggregate:window` 2.0, `incident:` 2.0,
  `aggregate:baseline` 1.0, `payment:` 0.5, anything else 1.0.
- valid refs = distinct cited refs present in the evidence (citation order); fabricated = the others.
  `support_score = Σ ref_weight(valid) − 2.0 × len(fabricated)`; `supported = valid non-empty and fabricated
  empty`.
- Order: `support_score` desc, then LLM order (when used), then input order.
- `resolution_steps`: `PLAYBOOK[cause_code]` (actionable steps, last one says how to confirm), then for each
  valid `incident:<id>` ref whose evidence summary contains `Fix: <text>`:
  `"Apply the fix from incident <id>: <text>"`. Unknown cause code →
  `("Collect more evidence before acting: no playbook entry for <code>.",)`.
  PLAYBOOK codes: issuer_outage_on_bin, acquirer_outage, elevated_failures, merchant_integration_error,
  insufficient_funds, do_not_honor, expired_card, incorrect_cvc, fraud_flagged, card_testing,
  cross_border_limit_for_kyc_tier.
- Duplicate `hypothesis_id` → `ValueError`. Empty input → empty ranking, no LLM call.
- LLM reply: `{"order": [ids], "rationales": {id: str ≤300}}`; valid only when `order` is a permutation of the
  input ids and rationale keys ⊆ ids. The LLM cannot add, drop or rewrite hypotheses or steps.

### 6.3 Deterministic fake and judge goldens

`ops_ai.judge.fake.FakeJudgeLlm(reply: str | None = None)`: an `LlmPort` that records requests. With
`reply` it returns it verbatim; without, it returns the rubric's own opinion read from the `RUBRIC_JSON:` line
the judge puts in the prompt (agrees with the rubric, so CI is deterministic). Model name `fake-judge`.

`services/ops-ai/eval/judge/disputes.json` (records + expected verdict) and
`services/ops-ai/eval/judge/hypotheses.json` (hypotheses + evidence + expected order) feed the suite metrics
`judge_dispute_agreement` and `judge_ranking_agreement` (fraction matching, threshold 1.0).

## 7. Make targets

- `make eval`: runs the evaluation and judge test suites (golden SQ1–SQ4 E2E, custom metrics, leakage gate)
  and prints the report (`EVAL_REPORT_PATH`). Needs Postgres and Redis (`make up`).
- `make ci` runs `eval` after `test`; any failed gate (including a single cross-tenant leak) fails CI.
  LLM-judged metrics run only with `OPENAI_API_KEY` set and are reported as skipped otherwise.
