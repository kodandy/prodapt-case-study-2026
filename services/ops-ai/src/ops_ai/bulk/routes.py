from collections.abc import Callable
from datetime import timedelta
from typing import Annotated, Literal, Self

from fastapi import APIRouter, Depends, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.assistant.llm_port import LlmPort
from ops_ai.bulk.analysis import DEFAULT_TOKEN_BUDGET, analyze_bulk
from ops_ai.bulk.service import load_rows, narrate_bulk
from ops_ai.scope import Scope

MAX_SPAN = timedelta(days=31)


class BulkAnalyzeRequest(BaseModel):
    """No tenant field: the merchant comes from the token only."""

    model_config = ConfigDict(extra="forbid")

    start: AwareDatetime
    end: AwareDatetime
    token_budget: Annotated[StrictInt, Field(ge=100, le=4000)] = DEFAULT_TOKEN_BUDGET

    @model_validator(mode="after")
    def _window(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end - self.start > MAX_SPAN:
            raise ValueError("the window must not exceed 31 days")
        return self


class BulkAnalyzeResponse(BaseModel):
    transactions: int
    chunks: int
    naive_tokens: int
    optimized_tokens: int
    reduction: float
    within_budget: bool
    prompt: str
    mode: Literal["llm", "t2_fallback"]
    text: str | None
    model: str | None


def create_bulk_router(
    session_factory: sessionmaker[Session], llm: LlmPort, merchant_scope: Callable[..., Scope]
) -> APIRouter:
    """POST /v1/bulk/analyze (merchant token): token-budgeted map-reduce summary of the
    merchant's payments."""
    router = APIRouter()

    @router.post("/v1/bulk/analyze")
    def post_bulk_analyze(
        body: BulkAnalyzeRequest, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> BulkAnalyzeResponse:
        with session_factory() as session:
            rows = load_rows(session, scope, body.start, body.end)
        result = narrate_bulk(analyze_bulk(rows, token_budget=body.token_budget), llm)
        response.headers["Cache-Control"] = "no-store"
        analysis = result.analysis
        return BulkAnalyzeResponse(
            transactions=analysis.transactions,
            chunks=analysis.chunks,
            naive_tokens=analysis.naive_tokens,
            optimized_tokens=analysis.optimized_tokens,
            reduction=analysis.reduction,
            within_budget=analysis.within_budget,
            prompt=analysis.prompt,
            mode=result.mode,
            text=result.text,
            model=result.model,
        )

    return router
