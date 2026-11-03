from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.assistant.llm_port import LlmPort
from ops_ai.assistant.service import AnswerMode, ask
from ops_ai.auth import InvalidTokenError, verify_merchant_token
from ops_ai.clock import Clock
from ops_ai.scope import Scope


def _always_ready() -> list[str]:
    return []


@dataclass(frozen=True)
class OpsAiAppDeps:
    session_factory: sessionmaker[Session]
    llm: LlmPort
    jwt_public_key_pem: str
    clock: Clock
    readiness: Callable[[], list[str]] = field(default=_always_ready)


class AskRequest(BaseModel):
    """No tenant field: the scope comes from the token only, so `merchant_id` here is a 422."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class EvidenceOut(BaseModel):
    ref: str
    summary: str


class AskResponse(BaseModel):
    mode: AnswerMode
    text: str | None
    model: str | None
    evidence: list[EvidenceOut]


def create_app(deps: OpsAiAppDeps) -> FastAPI:
    """Routes:
    - GET /healthz → 200 (liveness). GET /readyz → 200 ready, or 503 with the failed components.
    - POST /v1/assistant/ask (Authorization: Bearer <merchant token>) {question} → AskResponse.
      Missing/invalid/expired token → 401 {"detail": "invalid token"}.
    """
    app = FastAPI(title="ops-ai")
    app.add_exception_handler(RequestValidationError, _validation_error_without_input)
    bearer = HTTPBearer(auto_error=False)

    def scope_from_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Scope:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
        try:
            return verify_merchant_token(credentials.credentials, deps.jwt_public_key_pem, deps.clock())
        except InvalidTokenError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        failed = deps.readiness()
        if failed:
            return JSONResponse({"status": "not_ready", "failed": failed}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/assistant/ask")
    def assistant_ask(body: AskRequest, scope: Annotated[Scope, Depends(scope_from_token)]) -> AskResponse:
        with deps.session_factory() as session:
            answer = ask(session, scope, body.question, deps.llm)
        return AskResponse(
            mode=answer.mode,
            text=answer.text,
            model=answer.model,
            evidence=[EvidenceOut(ref=item.ref, summary=item.summary) for item in answer.evidence],
        )

    return app


async def _validation_error_without_input(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's default 422 echoes the rejected input back."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in errors]
    return JSONResponse({"error": "validation_failed", "detail": detail}, status_code=422)
