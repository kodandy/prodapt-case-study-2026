# R2-8 Predictive analytics dashboard and B13.3 ops console

Scope: settlement forecasting, chargeback probability and real-time anomaly alerts in `ops-ai`
(`services/ops-ai/src/ops_ai/analytics/`), a merchant dashboard (`web/src/features/dashboard/`) and the ops
console (`web/src/features/ops/`). Every model is small, deterministic and explainable: no new dependency, pure
Python, no LLM.

## 1. Settlement forecast (`ops_ai.analytics.forecast`)

Target: the merchant's daily captured card volume per currency (`payment_facts.state = 'succeeded'`), which is
the gross of the next payouts. Refunds and chargebacks are not subtracted (they are rare and lag the capture).

Model: simple exponential smoothing (level only). The synthetic dataset has no weekly seasonality (timestamps
are uniform over October), so a seasonal model only adds variance; `alpha` is fitted per series from the grid
`ALPHA_GRID = (0.1, 0.2, ..., 0.9)` by minimising the one-step-ahead squared error.

```python
@dataclass(frozen=True)
class DailyPoint: day: date; amount_minor: int
@dataclass(frozen=True)
class ForecastPoint: day: date; expected_minor: int; low_minor: int; high_minor: int
@dataclass(frozen=True)
class BacktestResult: horizon_days: int; folds: int; mae_minor: float; wape: float | None; naive_wape: float | None
@dataclass(frozen=True)
class CurrencyForecast:
    currency: str; alpha: float; history: tuple[DailyPoint, ...]; forecast: tuple[ForecastPoint, ...]
    backtest: BacktestResult | None

ALPHA_GRID: tuple[float, ...]
INTERVAL_Z = 1.96
def fill_daily(points: Sequence[DailyPoint], start: date, end: date) -> list[DailyPoint]
def fit_alpha(values: Sequence[int]) -> float
def forecast_series(history: Sequence[DailyPoint], horizon_days: int, alpha: float | None = None) -> list[ForecastPoint]
def backtest(history: Sequence[DailyPoint], horizon_days: int, folds: int) -> BacktestResult | None
def merchant_forecast(session: Session, scope: Scope, now: datetime, horizon_days: int, history_days: int = 28) -> list[CurrencyForecast]
```

- `fill_daily`: one point per day in `[start, end)` in order; missing days are 0; points outside are dropped.
- `fit_alpha`: fewer than 3 values → 0.5. Ties → the smallest alpha.
- `forecast_series`: empty history → `[]`. Level `l0 = v0`, `l_t = alpha*v_t + (1-alpha)*l_{t-1}`. Every forecast
  day (the `horizon_days` days after the last history day) has `expected_minor = round(level)`. The interval is
  `± INTERVAL_Z * rmse` of the one-step residuals (0 with fewer than 2 points), `low_minor` clipped at 0,
  both rounded to int. `alpha=None` → `fit_alpha`.
- `backtest`: rolling origin. Fold k (1..folds) trains on `history[: n - k*horizon]` and tests the next `horizon`
  days. Folds whose training part has fewer than 7 points are skipped; no fold left → `None`. `mae_minor` is the
  mean absolute error over all tested days; `wape = Σ|err| / Σ actual` (None when Σ actual = 0); `naive_wape`
  is the same for the last-value forecast (the baseline the model must be compared to).
- `merchant_forecast`: history is the `history_days` full UTC days before `now`'s day (today is partial and
  excluded); one `CurrencyForecast` per currency with any succeeded payment in that window, sorted by currency;
  backtest with `folds=3` and the same horizon. Only `scope.merchant_id`'s rows are read.

## 2. Chargeback probability (`ops_ai.analytics.chargeback`)

Logistic regression fitted by Newton (IRLS) with an L2 penalty, on four features:

| Feature | Dataset (training/eval) | Serving (payment_facts + settlement_entries) |
|---|---|---|
| `log_amount_usd` = ln(1 + amount in USD) | amount / `SCORING_UNITS_PER_USD` | same |
| `cross_border` | country_sender ≠ country_receiver | not in `PaymentEventV1` → `None`, imputed with the training mean |
| `prior_decline_24h` | same payer had a failed/flagged record in the 24 h before | same `card_ref` had a declined payment in the 24 h before |
| `merchant_cb_rate` | smoothed rate of the merchant's earlier captured payments | smoothed rate of the merchant's settlement entries captured before the payment (the payment itself excluded) |

Smoothed rate: `(chargebacks + PRIOR_WEIGHT * PRIOR_RATE) / (captured + PRIOR_WEIGHT)` with `PRIOR_RATE = 0.006`,
`PRIOR_WEIGHT = 50`.

```python
FEATURE_NAMES = ("log_amount_usd", "cross_border", "prior_decline_24h", "merchant_cb_rate")
@dataclass(frozen=True)
class ChargebackFeatures: amount_usd: float; cross_border: bool | None; prior_decline_24h: bool; merchant_cb_rate: float
@dataclass(frozen=True)
class LogisticModel: intercept: float; weights: tuple[float, ...]; cross_border_mean: float
@dataclass(frozen=True)
class LabeledRow: features: ChargebackFeatures; label: bool; occurred_at: datetime
@dataclass(frozen=True)
class CalibrationBin: lower: float; upper: float; count: int; mean_predicted: float; observed_rate: float
@dataclass(frozen=True)
class ChargebackEvaluation:
    model: LogisticModel; train_count: int; test_count: int; test_positives: int
    auc: float | None; brier: float; base_rate_brier: float; calibration: tuple[CalibrationBin, ...]

def feature_vector(model: LogisticModel, features: ChargebackFeatures) -> tuple[float, ...]
def score(model: LogisticModel, features: ChargebackFeatures) -> float          # in (0, 1)
def contributions(model: LogisticModel, features: ChargebackFeatures) -> dict[str, float]  # weight * value per FEATURE_NAMES
def fit_logistic(rows: Sequence[LabeledRow], l2: float = 1.0, iterations: int = 25) -> LogisticModel
def roc_auc(scores: Sequence[float], labels: Sequence[bool]) -> float             # ValueError when one class is absent
def brier(scores: Sequence[float], labels: Sequence[bool]) -> float
def calibration_bins(scores: Sequence[float], labels: Sequence[bool], bins: int = 5) -> list[CalibrationBin]
def dataset_rows(log: ScenarioLog) -> list[LabeledRow]
def evaluate_on_dataset(log: ScenarioLog, train_fraction: float = 0.7) -> ChargebackEvaluation
def default_evaluation() -> ChargebackEvaluation   # cached; generate(seed=42, records=12000)
```

- `roc_auc` is the Mann–Whitney statistic (ties count ½). `calibration_bins` splits [0, 1] into `bins` equal-width
  bins and returns only non-empty ones. `fit_logistic` does not penalise the intercept; `cross_border_mean` is the
  mean of the known `cross_border` values (0.0 if none).
- `dataset_rows`: merchant card payments with status `success` or `reversed`, ordered by (timestamp,
  transaction_id); every feature uses only earlier records. `evaluate_on_dataset` splits by time (first
  `train_fraction` trains), reports AUC (None when the test part has one class), Brier, the base-rate Brier
  (constant = training positive rate) and calibration on the test part.
- Honest expectation: the generator draws baseline chargebacks at p = 0.006 independent of the features; only the
  SQ4 merchant has an elevated rate. The holdout AUC is therefore close to 0.5–0.7 and the useful property is
  calibration (Brier ≤ base-rate Brier + 0.001). A separate unit test plants a real signal to show the model
  learns it (AUC > 0.8).

Serving: `score_merchant_payments(session, scope, now, model, limit, lookback_days=30) -> MerchantChargebackRisk`
scores the merchant's succeeded payments in `[now - lookback_days, now)`, most probable first, ties by
payment_id; `MerchantChargebackRisk(captured_count, chargeback_count, merchant_cb_rate, items: tuple[PaymentRisk,
...])`, `PaymentRisk(payment_id, amount_minor, currency, occurred_at, probability, contributions)`.
Counts come from `settlement_entries` captured before `now`.

## 3. Real-time anomaly alerts (`ops_ai.analytics.anomaly`)

Per merchant, 15-minute buckets (aligned to the epoch, UTC). On each **live** payment event the consumer calls
`observe_payment(session, clock, event)` after the projection and the card-testing watch. It reads the
merchant's `payment_facts` for the current bucket and the `LOOKBACK_BUCKETS = 24` buckets before it (6 h; empty
buckets count as zero attempts), and scores three metrics with an EWMA z-score:

Every metric needs ≥ `MIN_ATTEMPTS = 10` attempts in the current bucket, so the first few payments of a quiet
merchant never alert.

- `failure_rate` = declined / attempts (history buckets with 0 attempts are skipped for rate metrics),
  `min_std = 0.02`;
- `volume` = attempts per bucket, `min_std = 1.0`, spikes only;
- `decline_mix` = share of attempts with one `decline_code`, per code, `min_std = 0.02`; the code with the
  highest z is reported in `detail`.

```python
BUCKET = timedelta(minutes=15); LOOKBACK_BUCKETS = 24; EWMA_ALPHA = 0.3; Z_THRESHOLD = 3.0
MIN_ATTEMPTS = 10; MIN_HISTORY_BUCKETS = 4
AnomalyMetric = Literal["failure_rate", "volume", "decline_mix"]
@dataclass(frozen=True)
class BucketStats: start: datetime; attempts: int; declined: int; by_code: Mapping[str, int]
@dataclass(frozen=True)
class Anomaly: metric: AnomalyMetric; detail: str | None; observed: float; expected: float; zscore: float
def bucket_start(at: datetime) -> datetime
def ewma_zscore(history: Sequence[float], current: float, alpha: float = EWMA_ALPHA, min_std: float = 0.0) -> float | None
def detect(history: Sequence[BucketStats], current: BucketStats) -> list[Anomaly]
def observe_payment(session: Session, clock: Clock, event: PaymentEventV1) -> list[uuid.UUID]
def list_alerts(session: Session, scope: Scope | None, since: datetime | None, limit: int) -> list[AnomalyAlertView]
```

- `ewma_zscore`: fewer than `MIN_HISTORY_BUCKETS` values → None. Mean `m0 = h0`, `v0 = 0`; for each later value
  `d = x - m; m += alpha*d; v = (1-alpha)*(v + alpha*d*d)`. Result `(current - m) / max(sqrt(v), min_std)`
  (`min_std = 0` and `v = 0` → `inf` when current > m, `0.0` when equal).
- `detect` returns metrics with `zscore >= Z_THRESHOLD`, in the order failure_rate, volume, decline_mix.
- `observe_payment` (origin ≠ live → `[]`, nothing read) upserts one `anomaly_alerts` row per anomaly, keyed by
  `alert_key = f"{metric}:{merchant_id}:{bucket_start:%Y-%m-%dT%H:%M}:{detail or ''}"`; a repeat in the same
  bucket updates observed/expected/zscore/attempts/updated_at and keeps `alert_id` and `created_at`. Returns the
  alert ids. Does not commit.
- `list_alerts`: `scope=None` (ops) lists every merchant; ordered by `updated_at` desc, alert_id; `since` keeps
  `updated_at > since`.

Table `ops_ai.anomaly_alerts` (migration on top of `b12c4e7a9f31`): `alert_id` uuid pk, `alert_key` text unique,
`merchant_id` uuid indexed, `metric` varchar(16), `detail` varchar(64) null, `bucket_start`, `observed`,
`expected`, `zscore` (double), `attempts` int, `created_at`, `updated_at`.

## 4. API (`ops_ai.analytics.routes.create_analytics_router`)

All responses carry `Cache-Control: no-store`. Merchant routes take the merchant token (same 401/403 as the
other merchant routes); the merchant comes from the token only, a `merchant_id` query parameter on them is a 422.

| Route | Query | Response |
|---|---|---|
| `GET /v1/analytics/forecast` | `horizon_days` 1..30 (default 7) | `{generated_at, horizon_days, history_days, currencies: [{currency, alpha, history: [{day, amount_minor}], forecast: [{day, expected_minor, low_minor, high_minor}], backtest: {horizon_days, folds, mae_minor, wape, naive_wape} \| null}]}` |
| `GET /v1/analytics/chargeback-risk` | `limit` 1..100 (default 20) | `{captured_count, chargeback_count, merchant_chargeback_rate, model: {features, train_count, test_count, test_positives, auc, brier, base_rate_brier, trained_on}, items: [{payment_id, amount_minor, currency, occurred_at, probability, contributions}]}` |
| `GET /v1/analytics/alerts` | `since` (ISO 8601 with offset, optional), `limit` 1..200 (default 50) | `{server_time, items: [{alert_id, metric, detail, bucket_start, observed, expected, zscore, attempts, created_at, updated_at}]}` |
| `GET /v1/ops/analytics/alerts` | same | same, every item also has `merchant_id` |
| `GET /v1/ops/analytics/forecast` | `merchant_id` (required), `horizon_days` | as the merchant route |
| `GET /v1/ops/analytics/chargeback-risk` | `merchant_id` (required), `limit` | as the merchant route |

Ops routes are guarded by one function, `ops_ai.analytics.access.require_ops_principal(principal: Principal)
-> OpsAccess` (`OpsAccess(actor: str)`; raises `ForbiddenOpsError`), used through one FastAPI dependency
`analytics_ops_access` in `app.py`. It delegates to `ops_ai.scope.require_ops_role` with the default role set,
so the ops analytics routes follow the same ops principal as the other `/v1/ops/*` routes:

- an `OpsScope` with role `ops_agent` or `ops_lead` → `OpsAccess(actor=f"ops:{ops_user_id}")` and the route
  answers 200. Ops staff are cross-tenant by design: `/v1/ops/analytics/alerts` lists every merchant's alerts
  (each item names its `merchant_id`); `/v1/ops/analytics/forecast` and `/v1/ops/analytics/chargeback-risk`
  read the merchant named by the explicit `merchant_id` query parameter (required; missing or not a UUID →
  422, a merchant with no data → 200 with empty `currencies` / zero counts and empty `items`, the same shape
  as the merchant route). The response for a given `merchant_id` equals what that merchant's own token gets
  from the matching merchant route.
- any other principal (merchant `Scope`, `UserScope`) → `ForbiddenOpsError` → 403 `{"detail": "forbidden"}`.
  A merchant token never reaches another merchant's data through the ops routes.
- missing, invalid or expired token → 401 `{"detail": "invalid token"}` (from `principal_from_token`, before
  the guard runs).

The merchant routes are unchanged: an ops token on `/v1/analytics/*` → 403 `{"detail": "forbidden"}`.

## 5. Web

- `web/src/features/dashboard/`: `DashboardArea` (merchant sign-in via the existing merchant key flow, then the
  three panels), `ForecastPanel` (SVG chart per currency: history solid line, forecast dashed line with a shaded
  interval; `role="img"` with a text summary, plus a data table; backtest WAPE vs naive WAPE shown),
  `ChargebackRiskPanel` (table of payments, probability as %, top contribution, model AUC/Brier and the
  "synthetic labels" caveat), `AlertsPanel` (polls `/v1/analytics/alerts` every 15 s with `since`, newest first,
  `aria-live="polite"`). Chart palette: two series colours plus a band, chosen to keep ≥ 3:1 contrast on both
  light and dark backgrounds, defined as CSS variables.
- `web/src/features/ops/`: `OpsConsole` with a visible banner "Signed in with the merchant token: you see this
  merchant's data only (no ops role yet)", and panels: `FailureQueryPanel` (NL question → `POST
  /v1/assistant/investigate`; shows cause label, answer text, suggested fixes, evidence refs, tool calls),
  `FlaggedPanel` (`POST /v1/assistant/investigate-fraud` → flagged episodes and alerts), alerts (the dashboard
  `AlertsPanel`), `TicketQueuePanel` (`GET /v1/tickets`, filter by status, `Assign to me`/`Resolve` via `POST
  /v1/tickets/{id}/transition`; a 403 `ticket_not_merchant_owned` shows a message).
- `App.tsx` adds two areas: "Dashboard" and "Ops console".
