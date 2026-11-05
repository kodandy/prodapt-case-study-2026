from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ops_ai.assistant.tool_assistant import answer_with_tools
from ops_ai.investigation.failures import investigate_failures
from ops_ai.scope import Principal, Scope
from ops_ai.tools.catalog import merchant_tools, user_tools
from ops_ai.tools.planner import RuleBasedToolPlanner

if TYPE_CHECKING:
    from ops_ai.app import OpsAiAppDeps


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=2000)


def create_assistant_router(
    deps: "OpsAiAppDeps",
    merchant_scope: Callable[..., Scope],
    principal_from_token: Callable[..., Principal],
) -> APIRouter:
    router = APIRouter(prefix="/v1/assistant")

    @router.post("/failures")
    def failures(body: QuestionRequest, scope: Annotated[Scope, Depends(merchant_scope)]) -> JSONResponse:
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
        return JSONResponse(jsonable_encoder(payload), headers={"Cache-Control": "no-store"})

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
        else:
            answer = answer_with_tools(
                deps.session_factory, principal, body.question, deps.clock(), user_tools(), planner, deps.llm
            )
        return JSONResponse(jsonable_encoder(asdict(answer)), headers={"Cache-Control": "no-store"})

    return router
