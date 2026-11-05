import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.alerts.service import (
    AlertStatus,
    OpsAlertView,
    acknowledge_ops_alert,
    get_ops_alert,
    list_ops_alerts,
)
from ops_ai.clock import Clock


def create_alerts_router(
    session_factory: sessionmaker[Session], clock: Clock, staff_dependency: Callable[..., uuid.UUID]
) -> APIRouter:
    router = APIRouter(prefix="/v1/ops/alerts")

    @router.get("")
    def list_alerts(
        response: Response,
        staff_id: Annotated[uuid.UUID, Depends(staff_dependency)],
        status: AlertStatus = "open",
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, list[OpsAlertView]]:
        response.headers["Cache-Control"] = "no-store"
        with session_factory() as session:
            return {"alerts": list_ops_alerts(session, status=status, limit=limit)}

    @router.get("/{alert_id}")
    def detail(
        alert_id: uuid.UUID,
        response: Response,
        staff_id: Annotated[uuid.UUID, Depends(staff_dependency)],
    ) -> OpsAlertView:
        response.headers["Cache-Control"] = "no-store"
        with session_factory() as session:
            view = get_ops_alert(session, alert_id)
        if view is None:
            raise HTTPException(status_code=404, detail="alert not found")
        return view

    @router.post("/{alert_id}/ack")
    def acknowledge(
        alert_id: uuid.UUID,
        response: Response,
        staff_id: Annotated[uuid.UUID, Depends(staff_dependency)],
    ) -> OpsAlertView:
        response.headers["Cache-Control"] = "no-store"
        with session_factory.begin() as session:
            view = acknowledge_ops_alert(session, clock, alert_id, staff_id)
        if view is None:
            raise HTTPException(status_code=404, detail="alert not found")
        return view

    return router
