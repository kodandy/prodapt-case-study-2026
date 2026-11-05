import uuid
from collections.abc import Callable
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.analytics.access import OpsAccess
from ops_ai.analytics.anomaly import AnomalyAlertView, list_alerts
from ops_ai.analytics.chargeback import (
    DATASET_RECORDS,
    DATASET_SEED,
    DEFAULT_TRAIN_FRACTION,
    FEATURE_NAMES,
    MerchantChargebackRisk,
    default_evaluation,
    score_merchant_payments,
)
from ops_ai.analytics.forecast import DEFAULT_HISTORY_DAYS, CurrencyForecast, merchant_forecast
from ops_ai.clock import Clock
from ops_ai.scope import Scope

HORIZON_MAX = 30
RISK_LIMIT_MAX = 100
ALERTS_LIMIT_MAX = 200
NO_STORE = {"Cache-Control": "no-store"}
TRAINED_ON = (
    f"synthetic dataset (seed={DATASET_SEED}, records={DATASET_RECORDS}), "
    f"time split {round(DEFAULT_TRAIN_FRACTION * 100)}/{round((1 - DEFAULT_TRAIN_FRACTION) * 100)}"
)


class DailyPointOut(BaseModel):
    day: date
    amount_minor: int


class ForecastPointOut(BaseModel):
    day: date
    expected_minor: int
    low_minor: int
    high_minor: int


class BacktestOut(BaseModel):
    horizon_days: int
    folds: int
    mae_minor: float
    wape: float | None
    naive_wape: float | None


class CurrencyForecastOut(BaseModel):
    currency: str
    alpha: float
    history: list[DailyPointOut]
    forecast: list[ForecastPointOut]
    backtest: BacktestOut | None

    @classmethod
    def of(cls, item: CurrencyForecast) -> "CurrencyForecastOut":
        backtest = item.backtest
        return cls(
            currency=item.currency,
            alpha=item.alpha,
            history=[DailyPointOut(day=p.day, amount_minor=p.amount_minor) for p in item.history],
            forecast=[
                ForecastPointOut(
                    day=p.day, expected_minor=p.expected_minor, low_minor=p.low_minor, high_minor=p.high_minor
                )
                for p in item.forecast
            ],
            backtest=None
            if backtest is None
            else BacktestOut(
                horizon_days=backtest.horizon_days,
                folds=backtest.folds,
                mae_minor=backtest.mae_minor,
                wape=backtest.wape,
                naive_wape=backtest.naive_wape,
            ),
        )


class ForecastOut(BaseModel):
    generated_at: datetime
    horizon_days: int
    history_days: int
    currencies: list[CurrencyForecastOut]


class ModelOut(BaseModel):
    features: list[str]
    train_count: int
    test_count: int
    test_positives: int
    auc: float | None
    brier: float
    base_rate_brier: float
    trained_on: str


class RiskItemOut(BaseModel):
    payment_id: uuid.UUID
    amount_minor: int
    currency: str
    occurred_at: datetime
    probability: float
    contributions: dict[str, float]


class ChargebackRiskOut(BaseModel):
    captured_count: int
    chargeback_count: int
    merchant_chargeback_rate: float
    model: ModelOut
    items: list[RiskItemOut]


class AlertOut(BaseModel):
    alert_id: uuid.UUID
    metric: str
    detail: str | None
    bucket_start: datetime
    observed: float
    expected: float
    zscore: float
    attempts: int
    created_at: datetime
    updated_at: datetime


class OpsAlertOut(AlertOut):
    merchant_id: uuid.UUID


class AlertsOut(BaseModel):
    server_time: datetime
    items: list[AlertOut]


class OpsAlertsOut(BaseModel):
    server_time: datetime
    items: list[OpsAlertOut]


def _alert_fields(view: AnomalyAlertView) -> dict[str, object]:
    return {
        "alert_id": view.alert_id,
        "metric": view.metric,
        "detail": view.detail,
        "bucket_start": view.bucket_start,
        "observed": view.observed,
        "expected": view.expected,
        "zscore": view.zscore,
        "attempts": view.attempts,
        "created_at": view.created_at,
        "updated_at": view.updated_at,
    }


def _risk_out(risk: MerchantChargebackRisk) -> ChargebackRiskOut:
    evaluation = default_evaluation()
    return ChargebackRiskOut(
        captured_count=risk.captured_count,
        chargeback_count=risk.chargeback_count,
        merchant_chargeback_rate=risk.merchant_cb_rate,
        model=ModelOut(
            features=list(FEATURE_NAMES),
            train_count=evaluation.train_count,
            test_count=evaluation.test_count,
            test_positives=evaluation.test_positives,
            auc=evaluation.auc,
            brier=evaluation.brier,
            base_rate_brier=evaluation.base_rate_brier,
            trained_on=TRAINED_ON,
        ),
        items=[
            RiskItemOut(
                payment_id=item.payment_id,
                amount_minor=item.amount_minor,
                currency=item.currency,
                occurred_at=item.occurred_at,
                probability=item.probability,
                contributions=item.contributions,
            )
            for item in risk.items
        ],
    )


def create_analytics_router(
    session_factory: sessionmaker[Session],
    clock: Clock,
    merchant_scope: Callable[..., Scope],
    ops_access: Callable[..., OpsAccess],
) -> APIRouter:
    """R2-8 analytics: merchant routes scoped by the token; /v1/ops/analytics/* behind the ops guard."""
    router = APIRouter()

    def token_merchant(request: Request, scope: Annotated[Scope, Depends(merchant_scope)]) -> Scope:
        """Authenticates first, then refuses a merchant_id query: the merchant comes from the token only."""
        if "merchant_id" in request.query_params:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "merchant_id is taken from the token", headers=NO_STORE
            )
        return scope

    def forecast_for(scope: Scope, horizon_days: int) -> ForecastOut:
        now = clock()
        with session_factory() as session:
            currencies = merchant_forecast(session, scope, now, horizon_days, DEFAULT_HISTORY_DAYS)
        return ForecastOut(
            generated_at=now,
            horizon_days=horizon_days,
            history_days=DEFAULT_HISTORY_DAYS,
            currencies=[CurrencyForecastOut.of(item) for item in currencies],
        )

    def risk_for(scope: Scope, limit: int) -> ChargebackRiskOut:
        model = default_evaluation().model
        with session_factory() as session:
            risk = score_merchant_payments(session, scope, clock(), model, limit)
        return _risk_out(risk)

    def alerts_for(
        scope: Scope | None, since: datetime | None, limit: int
    ) -> tuple[datetime, list[AnomalyAlertView]]:
        now = clock()
        with session_factory() as session:
            return now, list_alerts(session, scope, since, limit)

    @router.get("/v1/analytics/forecast")
    def read_forecast(
        scope: Annotated[Scope, Depends(token_merchant)],
        response: Response,
        horizon_days: Annotated[int, Query(ge=1, le=HORIZON_MAX)] = 7,
    ) -> ForecastOut:
        response.headers["Cache-Control"] = "no-store"
        return forecast_for(scope, horizon_days)

    @router.get("/v1/analytics/chargeback-risk")
    def read_chargeback_risk(
        scope: Annotated[Scope, Depends(token_merchant)],
        response: Response,
        limit: Annotated[int, Query(ge=1, le=RISK_LIMIT_MAX)] = 20,
    ) -> ChargebackRiskOut:
        response.headers["Cache-Control"] = "no-store"
        return risk_for(scope, limit)

    @router.get("/v1/analytics/alerts")
    def read_alerts(
        scope: Annotated[Scope, Depends(token_merchant)],
        response: Response,
        since: Annotated[AwareDatetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=ALERTS_LIMIT_MAX)] = 50,
    ) -> AlertsOut:
        now, views = alerts_for(scope, since, limit)
        response.headers["Cache-Control"] = "no-store"
        return AlertsOut(server_time=now, items=[AlertOut.model_validate(_alert_fields(v)) for v in views])

    @router.get("/v1/ops/analytics/alerts")
    def read_ops_alerts(
        _access: Annotated[OpsAccess, Depends(ops_access)],
        response: Response,
        since: Annotated[AwareDatetime | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=ALERTS_LIMIT_MAX)] = 50,
    ) -> OpsAlertsOut:
        now, views = alerts_for(None, since, limit)
        response.headers["Cache-Control"] = "no-store"
        return OpsAlertsOut(
            server_time=now,
            items=[
                OpsAlertOut.model_validate({**_alert_fields(v), "merchant_id": v.merchant_id}) for v in views
            ],
        )

    @router.get("/v1/ops/analytics/forecast")
    def read_ops_forecast(
        _access: Annotated[OpsAccess, Depends(ops_access)],
        merchant_id: uuid.UUID,
        response: Response,
        horizon_days: Annotated[int, Query(ge=1, le=HORIZON_MAX)] = 7,
    ) -> ForecastOut:
        response.headers["Cache-Control"] = "no-store"
        return forecast_for(Scope(merchant_id=merchant_id), horizon_days)

    @router.get("/v1/ops/analytics/chargeback-risk")
    def read_ops_chargeback_risk(
        _access: Annotated[OpsAccess, Depends(ops_access)],
        merchant_id: uuid.UUID,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=RISK_LIMIT_MAX)] = 20,
    ) -> ChargebackRiskOut:
        response.headers["Cache-Control"] = "no-store"
        return risk_for(Scope(merchant_id=merchant_id), limit)

    return router
