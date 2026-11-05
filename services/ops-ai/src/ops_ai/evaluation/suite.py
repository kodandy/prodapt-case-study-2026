"""Full evaluation run: golden SQ1-SQ4 cases over the real API plus the suite metrics
(docs/specs/evaluation-judge.md section 5)."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort
from ops_ai.evaluation.failure import dataset_failure_cases, failure_prediction_report, load_failure_labels
from ops_ai.evaluation.fraud import fraud_prediction_report
from ops_ai.evaluation.golden import CaseResult, GoldenCase, Principal, load_golden_cases, score_case
from ops_ai.evaluation.leakage import LEAKAGE_METRIC, find_leaks, leakage_metric
from ops_ai.evaluation.metrics import MetricResult, gate
from ops_ai.evaluation.report import EvalReport
from ops_ai.judge.dispute import judge_dispute
from ops_ai.judge.fake import FakeJudgeLlm
from ops_ai.judge.hypotheses import rank_hypotheses
from ops_ai.judge.schemas import DisputeWorkflowRecord, Hypothesis, Verdict
from ops_ai.search.indexing import index_incidents
from ops_ai.seed import merchant_uuid, seed_dataset
from ops_ai.settlement.seed import seed_settlement
from ops_ai.transfers.seed import seed_transfers
from scripts.generate_dataset import ScenarioLog, generate

DATA_DIR = Path(__file__).resolve().parents[3] / "eval"
GOLDEN_DIR = DATA_DIR / "golden"
FAILURE_LABELS = DATA_DIR / "failure_labels.json"
JUDGE_DISPUTES = DATA_DIR / "judge" / "disputes.json"
JUDGE_HYPOTHESES = DATA_DIR / "judge" / "hypotheses.json"

DATASET_SEED = 42
DATASET_RECORDS = 12_000
QUIET_MERCHANT = "mch_0010"
FRAUD_THRESHOLD = 0.95
FAILURE_DATASET_THRESHOLD = 0.99
FAILURE_PARAPHRASE_THRESHOLD = 0.75
JUDGE_AGREEMENT_THRESHOLD = 1.0
TOKEN_TTL = timedelta(minutes=15)
LIVE_TAIL = timedelta(hours=24)
_FROZEN = ConfigDict(extra="forbid", frozen=True)


class _DisputeCase(BaseModel):
    model_config = _FROZEN
    case_id: str
    expected_verdict: Verdict
    record: DisputeWorkflowRecord


class _DisputeFile(BaseModel):
    model_config = _FROZEN
    version: int
    cases: tuple[_DisputeCase, ...]


class _EvidenceRow(BaseModel):
    model_config = _FROZEN
    ref: str
    summary: str


class _RankingCase(BaseModel):
    model_config = _FROZEN
    case_id: str
    evidence: tuple[_EvidenceRow, ...]
    hypotheses: tuple[Hypothesis, ...]
    expected_order: tuple[str, ...]


class _RankingFile(BaseModel):
    model_config = _FROZEN
    version: int
    cases: tuple[_RankingCase, ...]


class _KeyPair:
    def __init__(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        self.public_pem = (
            key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )


def _merchant_token(keys: _KeyPair, merchant_id: uuid.UUID, now: datetime) -> str:
    issued = int(now.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": issued,
        "exp": issued + int(TOKEN_TTL.total_seconds()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, keys.private_pem, algorithm="RS256")


def _user_token(keys: _KeyPair, user_id: uuid.UUID, now: datetime) -> str:
    issued = int(now.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"user:{user_id}",
        "user_id": str(user_id),
        "roles": ["consumer"],
        "iat": issued,
        "exp": issued + int(TOKEN_TTL.total_seconds()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, keys.private_pem, algorithm="RS256")


def _merchant_external_ids(log: ScenarioLog) -> frozenset[str]:
    ids = {tx.merchant_id for tx in log.transactions if tx.merchant_id is not None}
    ids.update(incident.merchant_id for incident in log.incidents if incident.merchant_id is not None)
    return frozenset(ids)


def _merchant_foreign(log: ScenarioLog, merchant_ext_id: str) -> frozenset[str]:
    """Other merchants' uuids, their payments, every P2P transfer and other merchants' incidents."""
    foreign = {str(merchant_uuid(other)) for other in _merchant_external_ids(log) if other != merchant_ext_id}
    foreign.update(str(tx.transaction_id) for tx in log.transactions if tx.merchant_id != merchant_ext_id)
    foreign.update(
        incident.incident_id
        for incident in log.incidents
        if incident.merchant_id is not None and incident.merchant_id != merchant_ext_id
    )
    return frozenset(foreign)


def _user_foreign(log: ScenarioLog, user_id: uuid.UUID) -> frozenset[str]:
    return frozenset(str(tx.transaction_id) for tx in log.transactions if tx.user_id != user_id)


def _sq2_sender(log: ScenarioLog) -> uuid.UUID:
    return next(tx.user_id for tx in log.transactions if tx.transaction_id == log.planted.sq2_transaction_id)


def _principals(
    log: ScenarioLog, keys: _KeyPair, now: datetime
) -> Mapping[Principal, tuple[str, frozenset[str]]]:
    """Each principal's bearer token and foreign set."""
    planted = log.planted
    merchants: dict[Principal, str] = {
        "sq1_merchant": planted.sq1_merchant_id,
        "sq3_merchant": planted.sq3_merchant_id,
        "sq4_merchant": planted.sq4_merchant_id,
        "quiet_merchant": QUIET_MERCHANT,
    }
    principals: dict[Principal, tuple[str, frozenset[str]]] = {
        name: (_merchant_token(keys, merchant_uuid(ext_id), now), _merchant_foreign(log, ext_id))
        for name, ext_id in merchants.items()
    }
    sender = _sq2_sender(log)
    principals["sq2_user"] = (_user_token(keys, sender, now), _user_foreign(log, sender))
    return principals


def _json_body(response_text: str) -> Mapping[str, object]:
    try:
        parsed: object = json.loads(response_text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _run_golden(
    client: TestClient,
    cases: tuple[GoldenCase, ...],
    principals: Mapping[Principal, tuple[str, frozenset[str]]],
    judge_llm: LlmPort | None,
) -> tuple[CaseResult, ...]:
    results: list[CaseResult] = []
    for case in cases:
        token, foreign = principals[case.principal]
        response = client.post(case.route, headers={"Authorization": f"Bearer {token}"}, json=case.body)
        body = _json_body(response.text)
        result = score_case(case, response.status_code, body, foreign, judge_llm)
        # The parsed body misses non-dict bodies and headers; a leak anywhere on the wire fails the gate.
        raw_leaks = find_leaks({"text": response.text, "headers": dict(response.headers)}, foreign)
        if raw_leaks:
            result = _with_leak_gate(result, leakage_metric(raw_leaks))
        results.append(result)
    return tuple(results)


def _with_leak_gate(result: CaseResult, leak_gate: MetricResult) -> CaseResult:
    metrics = tuple(leak_gate if metric.name == LEAKAGE_METRIC else metric for metric in result.metrics)
    return replace(result, metrics=metrics)


def _fraud_metrics(
    session_factory: sessionmaker[Session], log: ScenarioLog, anchor: datetime
) -> list[MetricResult]:
    report = fraud_prediction_report(session_factory, log, anchor)
    detail = f"tp={report.true_positive} fp={report.false_positive} fn={report.false_negative}"
    return [
        gate("fraud_precision", report.precision, FRAUD_THRESHOLD, detail),
        gate("fraud_recall", report.recall, FRAUD_THRESHOLD, detail),
        gate("fraud_f1", report.f1, FRAUD_THRESHOLD, detail),
    ]


def _failure_metrics(log: ScenarioLog) -> list[MetricResult]:
    labels = load_failure_labels(FAILURE_LABELS)
    dataset = failure_prediction_report(dataset_failure_cases(log, labels.reason_catalog))
    paraphrase = failure_prediction_report(labels.cases)
    return [
        gate(
            "failure_dataset_accuracy",
            dataset.accuracy,
            FAILURE_DATASET_THRESHOLD,
            f"{dataset.correct} of {dataset.total}",
        ),
        gate("failure_dataset_macro_f1", dataset.macro_f1, FAILURE_DATASET_THRESHOLD),
        gate(
            "failure_paraphrase_accuracy",
            paraphrase.accuracy,
            FAILURE_PARAPHRASE_THRESHOLD,
            f"{paraphrase.correct} of {paraphrase.total}",
        ),
    ]


def _agreement(name: str, matched: list[str], missed: list[str]) -> MetricResult:
    total = len(matched) + len(missed)
    score = len(matched) / total if total else 0.0
    detail = f"{len(matched)} of {total}" + (f"; disagreed: {', '.join(missed)}" if missed else "")
    return gate(name, score, JUDGE_AGREEMENT_THRESHOLD, detail)


def _judge_metrics(judge_llm: LlmPort) -> list[MetricResult]:
    disputes = _DisputeFile.model_validate_json(JUDGE_DISPUTES.read_text(encoding="utf-8"))
    matched: list[str] = []
    missed: list[str] = []
    for dispute in disputes.cases:
        verdict = judge_dispute(dispute.record, judge_llm).verdict
        (matched if verdict == dispute.expected_verdict else missed).append(dispute.case_id)
    dispute_metric = _agreement("judge_dispute_agreement", matched, missed)

    rankings = _RankingFile.model_validate_json(JUDGE_HYPOTHESES.read_text(encoding="utf-8"))
    matched, missed = [], []
    for ranking_case in rankings.cases:
        evidence = tuple(EvidenceItem(ref=row.ref, summary=row.summary) for row in ranking_case.evidence)
        ranking = rank_hypotheses(ranking_case.hypotheses, evidence, judge_llm)
        order = tuple(item.hypothesis_id for item in ranking.ranked)
        (matched if order == ranking_case.expected_order else missed).append(ranking_case.case_id)
    return [dispute_metric, _agreement("judge_ranking_agreement", matched, missed)]


def run_full_eval(
    session_factory: sessionmaker[Session], anchor: datetime, answer_llm: LlmPort, judge_llm: LlmPort | None
) -> EvalReport:
    """Seeds the dataset at `anchor`, runs every golden case through the real API and computes the suite
    metrics. `judge_llm` None → LLM-judged case metrics are skipped and the judge goldens use the fake."""
    log = generate(seed=DATASET_SEED, records=DATASET_RECORDS)
    # The last day arrives on the live stream, so the planted SQ3 burst raises its alert and ticket.
    seed_dataset(session_factory, lambda: anchor, log, anchor, live_since=anchor - LIVE_TAIL)
    with session_factory.begin() as session:
        index_incidents(session)
    seed_transfers(session_factory, log, anchor)
    seed_settlement(session_factory, log, anchor, lambda: anchor)

    keys = _KeyPair()
    app = create_app(
        OpsAiAppDeps(
            session_factory=session_factory,
            llm=answer_llm,
            jwt_public_key_pem=keys.public_pem,
            clock=lambda: anchor,
        )
    )
    with TestClient(app) as client:
        cases = _run_golden(client, load_golden_cases(GOLDEN_DIR), _principals(log, keys, anchor), judge_llm)

    suite_metrics = [
        *_fraud_metrics(session_factory, log, anchor),
        *_failure_metrics(log),
        *_judge_metrics(judge_llm if judge_llm is not None else FakeJudgeLlm()),
    ]
    return EvalReport(cases=cases, suite_metrics=tuple(suite_metrics))
