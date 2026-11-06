import uuid
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ops_ai.assistant.tool_assistant import answer_with_tools
from ops_ai.investigation.failures import investigate_failures
from ops_ai.scope import ForbiddenPrincipalError, Principal, Scope, UserScope, require_ops_role
from ops_ai.tools.catalog import merchant_tools, user_tools
from ops_ai.tools.planner import RuleBasedToolPlanner

if TYPE_CHECKING:
    from ops_ai.app import OpsAiAppDeps

NO_STORE = {"Cache-Control": "no-store"}
MERCHANT_ID_PARAM = "merchant_id"


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)


def create_assistant_router(
    deps: "OpsAiAppDeps",
    principal_from_token: Callable[..., Principal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/assistant")

    def failure_scope(
        request: Request, principal: Annotated[Principal, Depends(principal_from_token)]
    ) -> Scope:
        """Runs after authentication, so 401/403 come before any 422. A merchant token keeps its own
        merchant and may not name one; ops staff must name exactly one merchant; others get 403."""
        named = request.query_params.getlist(MERCHANT_ID_PARAM)
        if isinstance(principal, Scope):
            if named:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    "merchant_id is taken from the token",
                    headers=NO_STORE,
                )
            return principal
        try:
            require_ops_role(principal)
        except ForbiddenPrincipalError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from None
        return _ops_named_merchant(named)

    @router.post("/failures")
    def failures(body: QuestionRequest, scope: Annotated[Scope, Depends(failure_scope)]) -> JSONResponse:
        with deps.session_factory() as session:
            answer = investigate_failures(
                session, scope, body.question, deps.clock(), deps.llm, deps.tool_llm, deps.playbook
            )
        query = asdict(answer.query)
        query.pop("merchant_id")
        reasons = []
        for group in answer.breakdown.groups:
            reason = asdict(group)
            reason.pop("raw_codes")
            reasons.append(reason)
        payload = {
            "query": query,
            "abstained": answer.abstained,
            "mode": answer.mode,
            "text": answer.text,
            "model": answer.model,
            "comparison": asdict(answer.comparison),
            "timeline": asdict(answer.timeline),
            "reasons": reasons,
            "facts": [asdict(claim) for claim in answer.diagnosis.facts],
            "hypotheses": [asdict(claim) for claim in answer.diagnosis.hypotheses],
            "how_to_confirm": [asdict(claim) for claim in answer.diagnosis.how_to_confirm],
            "fixes": [asdict(fix) for fix in answer.diagnosis.fixes],
            "claims": [asdict(claim) for claim in answer.claims],
            "tool_calls": [asdict(call) for call in answer.tool_calls],
            "evidence": [asdict(item) for item in answer.evidence],
        }
        return JSONResponse(jsonable_encoder(payload), headers=NO_STORE)

    @router.post("/query")
    def query(
        body: QuestionRequest, principal: Annotated[Principal, Depends(principal_from_token)]
    ) -> JSONResponse:
        planner = deps.tool_llm or RuleBasedToolPlanner()
        if isinstance(principal, Scope):
            answer = answer_with_tools(
                deps.session_factory,
                principal,
                body.question,
                deps.clock(),
                merchant_tools(),
                planner,
                deps.llm,
            )
        elif isinstance(principal, UserScope):
            answer = answer_with_tools(
                deps.session_factory, principal, body.question, deps.clock(), user_tools(), planner, deps.llm
            )
        else:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return JSONResponse(jsonable_encoder(asdict(answer)), headers={"Cache-Control": "no-store"})

    return router


def _ops_named_merchant(named: list[str]) -> Scope:
    """The merchant an ops staff member named, as the scope that merchant's own token would carry. There is no
    implicit "all merchants": the failure investigation is per merchant. Only the canonical
    lower-case hyphenated form is accepted, the same rule auth applies to the merchant_id claim."""
    try:
        if len(named) != 1:
            raise ValueError
        merchant_id = uuid.UUID(named[0])
        if str(merchant_id) != named[0]:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "merchant_id is required for ops staff", headers=NO_STORE
        ) from None
    return Scope(merchant_id=merchant_id)
