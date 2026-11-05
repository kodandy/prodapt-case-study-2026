"""Tests for ops_ai.analytics.chargeback, written from docs/specs/analytics.md section 2."""

import math
import random
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from contracts.events import PaymentEventV1
from ops_ai.analytics import chargeback as chargeback_module
from ops_ai.analytics.chargeback import (
    FEATURE_NAMES,
    PRIOR_RATE,
    PRIOR_WEIGHT,
    ChargebackEvaluation,
    ChargebackFeatures,
    LabeledRow,
    LogisticModel,
    brier,
    calibration_bins,
    contributions,
    dataset_rows,
    default_evaluation,
    evaluate_on_dataset,
    feature_vector,
    fit_logistic,
    roc_auc,
    score,
    score_merchant_payments,
)
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.scope import Scope
from ops_ai.settlement.models import SettlementEntry
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW
from scripts.generate_dataset import CanonicalTransaction, ScenarioLog, generate

MERCHANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
MERCHANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000b2")
ROW_START = datetime(2026, 10, 1, tzinfo=UTC)


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def _features(
    *,
    amount_usd: float = 25.0,
    cross_border: bool | None = False,
    prior_decline_24h: bool = False,
    merchant_cb_rate: float = 0.01,
) -> ChargebackFeatures:
    return ChargebackFeatures(
        amount_usd=amount_usd,
        cross_border=cross_border,
        prior_decline_24h=prior_decline_24h,
        merchant_cb_rate=merchant_cb_rate,
    )


# --- constants -------------------------------------------------------------------------------------------


def test_feature_names_and_smoothing_prior_are_the_specified_ones() -> None:
    # Arrange / Act
    names, rate, weight = FEATURE_NAMES, PRIOR_RATE, PRIOR_WEIGHT

    # Assert
    assert names == ("log_amount_usd", "cross_border", "prior_decline_24h", "merchant_cb_rate")
    assert (rate, weight) == (0.006, 50)


# --- roc_auc ---------------------------------------------------------------------------------------------


def test_roc_auc_is_one_for_a_perfect_ranking() -> None:
    # Arrange
    scores = [0.1, 0.2, 0.8, 0.9]
    labels = [False, False, True, True]

    # Act
    auc = roc_auc(scores, labels)

    # Assert
    assert auc == pytest.approx(1.0)


def test_roc_auc_is_zero_for_an_inverted_ranking() -> None:
    # Arrange
    scores = [0.1, 0.2, 0.8, 0.9]
    labels = [True, True, False, False]

    # Act
    auc = roc_auc(scores, labels)

    # Assert
    assert auc == pytest.approx(0.0)


def test_roc_auc_counts_all_ties_as_one_half() -> None:
    # Arrange
    scores = [0.5, 0.5, 0.5, 0.5]
    labels = [True, False, True, False]

    # Act
    auc = roc_auc(scores, labels)

    # Assert
    assert auc == pytest.approx(0.5)


def test_roc_auc_is_the_fraction_of_correctly_ordered_positive_negative_pairs() -> None:
    # Arrange: positives 0.35 and 0.8, negatives 0.1 and 0.4 -> 3 of 4 pairs ordered
    scores = [0.1, 0.4, 0.35, 0.8]
    labels = [False, False, True, True]

    # Act
    auc = roc_auc(scores, labels)

    # Assert
    assert auc == pytest.approx(0.75)


def test_roc_auc_gives_half_credit_to_a_tied_pair_inside_a_larger_sample() -> None:
    # Arrange: positives 0.5 and 0.9, negatives 0.5 and 0.1 -> (0.5 + 1 + 1 + 1) / 4
    scores = [0.5, 0.9, 0.5, 0.1]
    labels = [True, True, False, False]

    # Act
    auc = roc_auc(scores, labels)

    # Assert
    assert auc == pytest.approx(0.875)


@pytest.mark.parametrize(
    "labels", [[True, True, True], [False, False, False]], ids=["only_positives", "only_negatives"]
)
def test_roc_auc_raises_value_error_when_one_class_is_absent(labels: list[bool]) -> None:
    # Arrange
    scores = [0.2, 0.5, 0.7]

    # Act / Assert
    with pytest.raises(ValueError):
        roc_auc(scores, labels)


# --- brier -----------------------------------------------------------------------------------------------


def test_brier_is_the_mean_squared_difference_between_score_and_label() -> None:
    # Arrange: (0.1^2 + 0.1^2 + 0.5^2) / 3
    scores = [0.9, 0.1, 0.5]
    labels = [True, False, False]

    # Act
    value = brier(scores, labels)

    # Assert
    assert value == pytest.approx(0.09)


def test_brier_is_zero_for_perfect_scores() -> None:
    # Arrange
    scores = [1.0, 0.0, 1.0]
    labels = [True, False, True]

    # Act
    value = brier(scores, labels)

    # Assert
    assert value == pytest.approx(0.0)


# --- calibration_bins ------------------------------------------------------------------------------------


def test_calibration_bins_returns_only_non_empty_bins_with_counts_and_means() -> None:
    # Arrange: five equal-width bins; bins 1 and 3 are empty
    scores = [0.05, 0.15, 0.5, 0.95, 0.85]
    labels = [False, True, False, True, True]

    # Act
    bins = calibration_bins(scores, labels)

    # Assert
    assert [b.count for b in bins] == [2, 1, 2]
    assert [b.lower for b in bins] == pytest.approx([0.0, 0.4, 0.8])
    assert [b.upper for b in bins] == pytest.approx([0.2, 0.6, 1.0])
    assert [b.mean_predicted for b in bins] == pytest.approx([0.10, 0.5, 0.90])
    assert [b.observed_rate for b in bins] == pytest.approx([0.5, 0.0, 1.0])


def test_calibration_bins_honours_the_bins_argument() -> None:
    # Arrange
    scores = [0.1, 0.3, 0.7]
    labels = [False, False, True]

    # Act
    bins = calibration_bins(scores, labels, bins=2)

    # Assert
    assert [(b.lower, b.upper, b.count) for b in bins] == [(0.0, 0.5, 2), (0.5, 1.0, 1)]
    assert [b.mean_predicted for b in bins] == pytest.approx([0.2, 0.7])
    assert [b.observed_rate for b in bins] == pytest.approx([0.0, 1.0])


def test_calibration_bins_counts_sum_to_the_sample_size() -> None:
    # Arrange
    rng = random.Random(11)  # noqa: S311 - reproducible test data
    scores = [rng.random() for _ in range(200)]
    labels = [rng.random() < s for s in scores]

    # Act
    bins = calibration_bins(scores, labels, bins=10)

    # Assert
    assert sum(b.count for b in bins) == 200
    assert [b.lower for b in bins] == sorted(b.lower for b in bins)


# --- feature_vector / score / contributions --------------------------------------------------------------


def _model(cross_border_mean: float = 0.25) -> LogisticModel:
    return LogisticModel(intercept=-2.0, weights=(0.1, 0.5, 1.0, 3.0), cross_border_mean=cross_border_mean)


def test_feature_vector_is_log1p_amount_cross_border_prior_decline_and_merchant_rate() -> None:
    # Arrange
    features = _features(amount_usd=9.0, cross_border=True, prior_decline_24h=True, merchant_cb_rate=0.2)

    # Act
    vector = feature_vector(_model(), features)

    # Assert
    assert vector == pytest.approx((math.log(10.0), 1.0, 1.0, 0.2))


def test_feature_vector_encodes_false_flags_as_zero() -> None:
    # Arrange
    features = _features(amount_usd=0.0, cross_border=False, prior_decline_24h=False, merchant_cb_rate=0.0)

    # Act
    vector = feature_vector(_model(), features)

    # Assert
    assert vector == pytest.approx((0.0, 0.0, 0.0, 0.0))


def test_feature_vector_imputes_an_unknown_cross_border_with_the_model_mean() -> None:
    # Arrange
    features = _features(cross_border=None)

    # Act
    vector = feature_vector(_model(cross_border_mean=0.25), features)

    # Assert
    assert vector[1] == pytest.approx(0.25)


def test_score_is_the_logistic_of_the_intercept_plus_the_weighted_features() -> None:
    # Arrange
    features = _features(amount_usd=9.0, cross_border=True, prior_decline_24h=False, merchant_cb_rate=0.2)
    logit = -2.0 + 0.1 * math.log(10.0) + 0.5 * 1.0 + 1.0 * 0.0 + 3.0 * 0.2

    # Act
    probability = score(_model(), features)

    # Assert
    assert probability == pytest.approx(_sigmoid(logit))


def test_score_stays_strictly_between_zero_and_one() -> None:
    # Arrange
    low = _features(amount_usd=0.0, cross_border=False, merchant_cb_rate=0.0)
    high = _features(amount_usd=2000.0, cross_border=True, prior_decline_24h=True, merchant_cb_rate=1.0)

    # Act
    low_score = score(_model(), low)
    high_score = score(_model(), high)

    # Assert
    assert 0.0 < low_score < high_score < 1.0


def test_score_is_strictly_increasing_in_the_merchant_rate_when_its_weight_is_positive() -> None:
    # Arrange
    rates = [0.0, 0.01, 0.05, 0.2, 0.5]

    # Act
    scores = [score(_model(), _features(merchant_cb_rate=r)) for r in rates]

    # Assert
    assert scores == sorted(scores)
    assert len(set(scores)) == len(rates)
    assert all(0.0 < s < 1.0 for s in scores)


def test_score_with_an_unknown_cross_border_equals_the_score_with_the_mean_plugged_in() -> None:
    # Arrange
    model = _model(cross_border_mean=0.25)
    unknown = _features(cross_border=None, amount_usd=40.0, merchant_cb_rate=0.03)
    vector = (math.log1p(40.0), 0.25, 0.0, 0.03)
    logit = model.intercept + sum(w * x for w, x in zip(model.weights, vector, strict=True))

    # Act
    probability = score(model, unknown)

    # Assert
    assert probability == pytest.approx(_sigmoid(logit))


def test_score_with_an_unknown_cross_border_lies_between_the_known_values_for_a_positive_weight() -> None:
    # Arrange
    model = _model(cross_border_mean=0.25)

    # Act
    never = score(model, _features(cross_border=False))
    imputed = score(model, _features(cross_border=None))
    always = score(model, _features(cross_border=True))

    # Assert
    assert never < imputed < always


def test_contributions_has_one_weight_times_value_entry_per_feature_name_in_order() -> None:
    # Arrange
    features = _features(amount_usd=9.0, cross_border=True, prior_decline_24h=True, merchant_cb_rate=0.2)

    # Act
    result = contributions(_model(), features)

    # Assert
    assert list(result) == list(FEATURE_NAMES)
    assert result == pytest.approx(
        {
            "log_amount_usd": 0.1 * math.log(10.0),
            "cross_border": 0.5,
            "prior_decline_24h": 1.0,
            "merchant_cb_rate": 0.6,
        }
    )


def test_contributions_plus_the_intercept_is_the_logit_of_the_score() -> None:
    # Arrange
    model = _model()
    features = _features(amount_usd=120.0, cross_border=None, prior_decline_24h=True, merchant_cb_rate=0.04)

    # Act
    result = contributions(model, features)

    # Assert
    assert score(model, features) == pytest.approx(_sigmoid(model.intercept + sum(result.values())))


# --- fit_logistic ----------------------------------------------------------------------------------------


def _planted_rows(count: int, seed: int) -> list[LabeledRow]:
    """The label depends on merchant_cb_rate only: logit = -4 + 8 * rate."""
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    rows: list[LabeledRow] = []
    for index in range(count):
        rate = rng.uniform(0.0, 1.0)
        features = ChargebackFeatures(
            amount_usd=rng.uniform(5.0, 500.0),
            cross_border=rng.random() < 0.3,
            prior_decline_24h=rng.random() < 0.1,
            merchant_cb_rate=rate,
        )
        label = rng.random() < _sigmoid(-4.0 + 8.0 * rate)
        occurred_at = ROW_START + timedelta(minutes=index)
        rows.append(LabeledRow(features=features, label=label, occurred_at=occurred_at))
    return rows


def test_fit_logistic_learns_a_planted_signal_and_ranks_the_holdout_with_auc_above_0_8() -> None:
    # Arrange
    rows = _planted_rows(2000, seed=7)
    train, holdout = rows[:1400], rows[1400:]

    # Act
    model = fit_logistic(train)

    # Assert
    holdout_scores = [score(model, r.features) for r in holdout]
    assert roc_auc(holdout_scores, [r.label for r in holdout]) > 0.8
    assert len(model.weights) == len(FEATURE_NAMES)
    assert model.weights[3] > 3.0
    assert all(abs(w) < model.weights[3] for w in model.weights[:3])
    assert model.intercept < -1.0


def test_fit_logistic_is_deterministic() -> None:
    # Arrange
    rows = _planted_rows(300, seed=5)

    # Act
    first = fit_logistic(rows)
    second = fit_logistic(rows)

    # Assert
    assert first == second


def _row(cross_border: bool | None, label: bool, minute: int) -> LabeledRow:
    return LabeledRow(
        features=_features(cross_border=cross_border, amount_usd=10.0 + minute),
        label=label,
        occurred_at=ROW_START + timedelta(minutes=minute),
    )


def test_fit_logistic_cross_border_mean_is_the_mean_of_the_known_values_only() -> None:
    # Arrange: known values True, False, True -> 2/3; the unknown rows do not count
    rows = [
        _row(True, True, 0),
        _row(False, False, 1),
        _row(None, False, 2),
        _row(True, False, 3),
        _row(None, True, 4),
        _row(False, False, 5),
    ]
    known = [r.features.cross_border for r in rows if r.features.cross_border is not None]

    # Act
    model = fit_logistic(rows)

    # Assert
    assert model.cross_border_mean == pytest.approx(sum(known) / len(known))


def test_fit_logistic_cross_border_mean_is_zero_when_no_value_is_known() -> None:
    # Arrange
    rows = [_row(None, i % 2 == 0, i) for i in range(6)]

    # Act
    model = fit_logistic(rows)

    # Assert
    assert model.cross_border_mean == 0.0


# --- dataset_rows ----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def small_log() -> ScenarioLog:
    return generate(seed=42, records=1200)


@pytest.fixture(scope="module")
def full_log() -> ScenarioLog:
    return generate(seed=42)


@pytest.fixture(scope="module")
def full_rows(full_log: ScenarioLog) -> list[LabeledRow]:
    return dataset_rows(full_log)


@pytest.fixture(scope="module")
def full_evaluation(full_log: ScenarioLog) -> ChargebackEvaluation:
    return evaluate_on_dataset(full_log)


def _captured_card_payments(log: ScenarioLog) -> list[CanonicalTransaction]:
    selected = [
        tx
        for tx in log.transactions
        if tx.record_kind == "merchant_payment"
        and tx.payment_method == "card"
        and tx.status in ("success", "reversed")
    ]
    return sorted(selected, key=lambda tx: (tx.timestamp, tx.transaction_id))


def test_dataset_rows_keeps_only_merchant_card_success_and_reversed_in_time_order(
    small_log: ScenarioLog,
) -> None:
    # Arrange
    expected = _captured_card_payments(small_log)

    # Act
    rows = dataset_rows(small_log)

    # Assert
    assert [r.occurred_at for r in rows] == [tx.timestamp for tx in expected]
    assert [r.label for r in rows] == [tx.chargeback_flag for tx in expected]
    assert len(rows) == len(expected)
    assert any(r.label for r in rows)


def test_dataset_rows_cross_border_is_sender_country_differing_from_receiver_country(
    small_log: ScenarioLog,
) -> None:
    # Arrange
    expected = [tx.country_sender != tx.country_receiver for tx in _captured_card_payments(small_log)]

    # Act
    rows = dataset_rows(small_log)

    # Assert
    assert [r.features.cross_border for r in rows] == expected
    assert True in expected
    assert False in expected


def test_dataset_rows_amount_is_the_usd_amount_for_usd_payments(small_log: ScenarioLog) -> None:
    # Arrange
    pairs = list(zip(_captured_card_payments(small_log), dataset_rows(small_log), strict=True))
    usd_pairs = [(tx, row) for tx, row in pairs if tx.currency == "USD"]

    # Act
    amounts = [row.features.amount_usd for _, row in usd_pairs]

    # Assert
    assert amounts == pytest.approx([tx.amount_minor / 100 for tx, _ in usd_pairs])
    assert len(usd_pairs) > 10


def test_dataset_rows_first_row_of_every_merchant_has_the_prior_rate_and_never_sees_the_future(
    small_log: ScenarioLog,
) -> None:
    # Arrange
    pairs = list(zip(_captured_card_payments(small_log), dataset_rows(small_log), strict=True))

    # Act
    seen: set[str | None] = set()
    first_rates: list[float] = []
    for tx, row in pairs:
        if tx.merchant_id not in seen:
            seen.add(tx.merchant_id)
            first_rates.append(row.features.merchant_cb_rate)

    # Assert: no earlier payment, so the smoothed rate is the prior (up to float rounding)
    assert len(first_rates) > 10
    assert first_rates == pytest.approx([PRIOR_RATE] * len(first_rates), rel=1e-9)


def test_dataset_rows_merchant_rate_uses_only_the_merchants_earlier_captured_payments(
    small_log: ScenarioLog,
) -> None:
    # Arrange
    pairs = list(zip(_captured_card_payments(small_log), dataset_rows(small_log), strict=True))
    captured: dict[str | None, int] = {}
    chargebacks: dict[str | None, int] = {}
    expected: list[float] = []
    for tx, _ in pairs:
        n = captured.get(tx.merchant_id, 0)
        c = chargebacks.get(tx.merchant_id, 0)
        expected.append((c + PRIOR_WEIGHT * PRIOR_RATE) / (n + PRIOR_WEIGHT))
        captured[tx.merchant_id] = n + 1
        chargebacks[tx.merchant_id] = c + int(tx.chargeback_flag)

    # Act
    actual = [row.features.merchant_cb_rate for _, row in pairs]

    # Assert
    assert actual == pytest.approx(expected, rel=1e-9)


# --- evaluate_on_dataset / default_evaluation ------------------------------------------------------------


def test_evaluate_on_dataset_splits_by_time_and_accounts_for_every_row(
    full_rows: list[LabeledRow], full_evaluation: ChargebackEvaluation
) -> None:
    # Arrange
    evaluation = full_evaluation

    # Act
    test_part = full_rows[evaluation.train_count :]

    # Assert
    assert evaluation.train_count + evaluation.test_count == len(full_rows)
    assert abs(evaluation.train_count - 0.7 * len(full_rows)) <= 1
    assert evaluation.test_positives == sum(r.label for r in test_part)


def test_evaluate_on_dataset_fits_the_model_on_the_training_part_only(
    full_rows: list[LabeledRow], full_evaluation: ChargebackEvaluation
) -> None:
    # Arrange
    train = full_rows[: full_evaluation.train_count]

    # Act
    refit = fit_logistic(train)

    # Assert
    assert full_evaluation.model == refit


def test_evaluate_on_dataset_brier_is_not_worse_than_the_base_rate_brier_by_more_than_0_001(
    full_evaluation: ChargebackEvaluation,
) -> None:
    # Arrange
    evaluation = full_evaluation

    # Act
    margin = evaluation.brier - evaluation.base_rate_brier

    # Assert
    assert margin <= 0.001


def test_evaluate_on_dataset_base_rate_brier_uses_the_training_positive_rate_as_a_constant(
    full_rows: list[LabeledRow], full_evaluation: ChargebackEvaluation
) -> None:
    # Arrange
    train = full_rows[: full_evaluation.train_count]
    rate = sum(r.label for r in train) / len(train)
    n, k = full_evaluation.test_count, full_evaluation.test_positives

    # Act
    expected = ((n - k) * rate**2 + k * (1 - rate) ** 2) / n

    # Assert
    assert full_evaluation.base_rate_brier == pytest.approx(expected)


def test_evaluate_on_dataset_reports_brier_and_auc_of_the_model_on_the_test_part(
    full_rows: list[LabeledRow], full_evaluation: ChargebackEvaluation
) -> None:
    # Arrange
    test_part = full_rows[full_evaluation.train_count :]
    scores = [score(full_evaluation.model, r.features) for r in test_part]
    labels = [r.label for r in test_part]

    # Act
    expected_brier = brier(scores, labels)

    # Assert
    assert full_evaluation.brier == pytest.approx(expected_brier)
    assert (full_evaluation.test_positives > 0) is (full_evaluation.auc is not None)
    if full_evaluation.auc is not None:
        assert full_evaluation.auc == pytest.approx(roc_auc(scores, labels))
        assert 0.0 <= full_evaluation.auc <= 1.0


def test_evaluate_on_dataset_calibration_covers_the_test_part_in_ascending_bins(
    full_evaluation: ChargebackEvaluation,
) -> None:
    # Arrange
    bins = full_evaluation.calibration

    # Act
    total = sum(b.count for b in bins)

    # Assert
    assert total == full_evaluation.test_count
    assert [b.lower for b in bins] == sorted(b.lower for b in bins)
    assert all(b.count > 0 for b in bins)


def test_evaluate_on_dataset_honours_the_train_fraction(small_log: ScenarioLog) -> None:
    # Arrange
    rows = dataset_rows(small_log)

    # Act
    evaluation = evaluate_on_dataset(small_log, train_fraction=0.5)

    # Assert
    assert evaluation.train_count + evaluation.test_count == len(rows)
    assert abs(evaluation.train_count - 0.5 * len(rows)) <= 1


def test_default_evaluation_is_cached_and_equals_the_evaluation_of_seed_42_with_12000_records(
    full_evaluation: ChargebackEvaluation,
) -> None:
    # Arrange / Act
    first = default_evaluation()
    second = default_evaluation()

    # Assert
    assert first is second
    assert first == full_evaluation


# --- score_merchant_payments (DB) ------------------------------------------------------------------------


def _payment(
    n: int,
    merchant_id: uuid.UUID,
    at: datetime,
    *,
    declined: bool = False,
    card: int | None = None,
    amount_minor: int = 2500,
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=10**9 + n),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=at,
        origin="backfill",
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="do_not_honor" if declined else None,
        card_bin="424242",
        card_last4="4242",
        card_ref=None if card is None else f"{card:032x}",
    )


def _entry(n: int, merchant_id: uuid.UUID, captured_at: datetime, *, chargeback: bool) -> SettlementEntry:
    return SettlementEntry(
        payment_id=uuid.UUID(int=50_000 + n),
        merchant_id=merchant_id,
        amount_minor=2500,
        currency="USD",
        captured_at=captured_at,
        statement_period="2026-09",
        refunded=False,
        chargeback=chargeback,
        ingest_source="seed",
    )


P_PLAIN, P_PRIOR, P_OLD_DECLINE, P_BOUNDARY = 1, 2, 3, 7


@pytest.fixture
def risk_data(session_factory: sessionmaker[Session]) -> None:
    long_ago = TEST_NOW - timedelta(days=60)
    day = timedelta(days=1)
    events = [
        _payment(P_PLAIN, MERCHANT_A, TEST_NOW - 2 * day),
        _payment(P_PRIOR, MERCHANT_A, TEST_NOW - day, card=1),
        _payment(100, MERCHANT_A, TEST_NOW - day - timedelta(hours=2), declined=True, card=1),
        _payment(P_OLD_DECLINE, MERCHANT_A, TEST_NOW - day, card=2),
        _payment(101, MERCHANT_A, TEST_NOW - day - timedelta(hours=30), declined=True, card=2),
        _payment(4, MERCHANT_A, TEST_NOW - day, declined=True, card=3),
        _payment(5, MERCHANT_A, TEST_NOW - timedelta(days=31)),
        _payment(6, MERCHANT_A, TEST_NOW),
        _payment(P_BOUNDARY, MERCHANT_A, TEST_NOW - timedelta(days=30)),
        _payment(8, MERCHANT_B, TEST_NOW - day),
    ]
    entries = [_entry(i, MERCHANT_A, long_ago, chargeback=i < 2) for i in range(20)]
    entries.append(_entry(20, MERCHANT_A, TEST_NOW + timedelta(hours=1), chargeback=True))
    entries.extend(_entry(30 + i, MERCHANT_B, long_ago, chargeback=True) for i in range(10))
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
        session.add_all(entries)


RISK_MODEL = LogisticModel(intercept=-5.0, weights=(0.25, 1.0, 2.0, 4.0), cross_border_mean=0.5)
MERCHANT_RATE = (2 + PRIOR_WEIGHT * PRIOR_RATE) / (20 + PRIOR_WEIGHT)


def _plain_logit() -> float:
    return -5.0 + 0.25 * math.log1p(25.0) + 1.0 * 0.5 + 4.0 * MERCHANT_RATE


def test_score_merchant_payments_scores_own_succeeded_payments_in_the_lookback_window_most_probable_first(
    session_factory: sessionmaker[Session], risk_data: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, scope, TEST_NOW, RISK_MODEL, limit=10)

    # Assert: the declined, the 31-day-old, the exactly-now and merchant B's payments are not scored;
    # the payment at now - 30 days is. Equal probabilities are ordered by payment_id.
    assert [item.payment_id for item in risk.items] == [
        uuid.UUID(int=P_PRIOR),
        uuid.UUID(int=P_PLAIN),
        uuid.UUID(int=P_OLD_DECLINE),
        uuid.UUID(int=P_BOUNDARY),
    ]


def test_score_merchant_payments_a_prior_decline_on_the_same_card_in_24h_raises_the_probability(
    session_factory: sessionmaker[Session], risk_data: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, scope, TEST_NOW, RISK_MODEL, limit=10)

    # Assert: P_PRIOR had a decline 2 h earlier; P_OLD_DECLINE's decline was 30 h earlier (outside 24 h)
    by_id = {item.payment_id: item for item in risk.items}
    assert by_id[uuid.UUID(int=P_PRIOR)].probability == pytest.approx(_sigmoid(_plain_logit() + 2.0))
    assert by_id[uuid.UUID(int=P_OLD_DECLINE)].probability == pytest.approx(_sigmoid(_plain_logit()))
    assert by_id[uuid.UUID(int=P_PLAIN)].probability == pytest.approx(_sigmoid(_plain_logit()))
    assert by_id[uuid.UUID(int=P_PRIOR)].probability > by_id[uuid.UUID(int=P_PLAIN)].probability


def test_score_merchant_payments_reports_amount_currency_time_and_contributions_per_item(
    session_factory: sessionmaker[Session], risk_data: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, scope, TEST_NOW, RISK_MODEL, limit=10)

    # Assert: serving has no cross_border, so the model mean (0.5) is imputed
    item = next(i for i in risk.items if i.payment_id == uuid.UUID(int=P_PRIOR))
    assert (item.amount_minor, item.currency, item.occurred_at) == (2500, "USD", TEST_NOW - timedelta(days=1))
    assert list(item.contributions) == list(FEATURE_NAMES)
    assert item.contributions == pytest.approx(
        {
            "log_amount_usd": 0.25 * math.log1p(25.0),
            "cross_border": 0.5,
            "prior_decline_24h": 2.0,
            "merchant_cb_rate": 4.0 * MERCHANT_RATE,
        }
    )


def test_score_merchant_payments_counts_come_from_own_settlement_entries_captured_before_now(
    session_factory: sessionmaker[Session], risk_data: None
) -> None:
    # Arrange: merchant A has 20 earlier entries (2 chargebacks) and one later chargeback; B has 10
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, scope, TEST_NOW, RISK_MODEL, limit=10)

    # Assert
    assert (risk.captured_count, risk.chargeback_count) == (20, 2)
    assert risk.merchant_cb_rate == pytest.approx(MERCHANT_RATE)


def test_score_merchant_payments_limit_keeps_the_most_probable_items_but_not_the_counts(
    session_factory: sessionmaker[Session], risk_data: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, scope, TEST_NOW, RISK_MODEL, limit=2)

    # Assert
    assert [item.payment_id for item in risk.items] == [uuid.UUID(int=P_PRIOR), uuid.UUID(int=P_PLAIN)]
    assert risk.captured_count == 20


def test_score_merchant_payments_lookback_days_narrows_the_window(
    session_factory: sessionmaker[Session], risk_data: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, scope, TEST_NOW, RISK_MODEL, limit=10, lookback_days=1)

    # Assert: only the payments from [now - 1 day, now): P_PRIOR and P_OLD_DECLINE
    assert [item.payment_id for item in risk.items] == [uuid.UUID(int=P_PRIOR), uuid.UUID(int=P_OLD_DECLINE)]


def test_score_merchant_payments_for_a_merchant_without_data_uses_the_prior_rate(
    session_factory: sessionmaker[Session], risk_data: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=uuid.UUID(int=999))

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, scope, TEST_NOW, RISK_MODEL, limit=10)

    # Assert
    assert risk.items == ()
    assert (risk.captured_count, risk.chargeback_count) == (0, 0)
    assert risk.merchant_cb_rate == pytest.approx(PRIOR_RATE)


# --- score_merchant_payments scores a bounded number of payments -------------------------------------------


def test_max_scored_payments_is_2000() -> None:
    # Arrange / Act
    limit = chargeback_module.MAX_SCORED_PAYMENTS

    # Assert
    assert limit == 2000


def test_score_merchant_payments_scores_only_the_most_recent_max_scored_payments_and_counts_all_entries(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: five succeeded payments; the three most recent carry the highest payment ids so that a cut
    # by payment id (or by insertion order) would pick a different set
    merchant = uuid.UUID(int=777)
    hour = timedelta(hours=1)
    events = [
        _payment(1, merchant, TEST_NOW - 5 * hour),
        _payment(2, merchant, TEST_NOW - 4 * hour),
        _payment(5, merchant, TEST_NOW - 3 * hour),
        _payment(3, merchant, TEST_NOW - 2 * hour),
        _payment(4, merchant, TEST_NOW - hour),
    ]
    entries = [_entry(i, merchant, TEST_NOW - timedelta(days=60), chargeback=i < 2) for i in range(10)]
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
        session.add_all(entries)
    monkeypatch.setattr(chargeback_module, "MAX_SCORED_PAYMENTS", 3)

    # Act
    with session_factory() as session:
        risk = score_merchant_payments(session, Scope(merchant_id=merchant), TEST_NOW, RISK_MODEL, limit=100)

    # Assert
    assert {item.payment_id for item in risk.items} == {
        uuid.UUID(int=5),
        uuid.UUID(int=3),
        uuid.UUID(int=4),
    }
    assert (risk.captured_count, risk.chargeback_count) == (10, 2)
