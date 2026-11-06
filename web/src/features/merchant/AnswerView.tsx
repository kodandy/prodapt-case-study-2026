import type { ReactNode } from "react";
import { GroundedCitations } from "./GroundedCitations";
import type { AssistantAnswer } from "./types";

const PAYMENT_REF = /^payment:([0-9a-f-]{36})$/;

/** Plain text only: no markdown and no HTML from the model. Evidence can open a known payment, nothing else. */
export function AnswerView(props: {
  answer: AssistantAnswer;
  knownPaymentIds: ReadonlySet<string>;
  onOpenPayment: (paymentId: string) => void;
}): ReactNode {
  const { answer } = props;
  return (
    <div className="answer" data-testid="assistant-answer">
      <p className="muted">
        Mode: {answer.mode === "llm" ? `LLM${answer.model === null ? "" : ` (${answer.model})`}` : "deterministic fallback"}
      </p>
      {answer.text === null ? null : <p className="answer-text">{answer.text}</p>}
      {answer.grounded === undefined ? null : <GroundedCitations answer={answer} />}
      {answer.suggested_fixes === undefined || answer.suggested_fixes.length === 0 ? null : (
        <>
          <h4>Suggested fixes</h4>
          <ul>
            {answer.suggested_fixes.map((fix) => (
              <li key={fix}>{fix}</li>
            ))}
          </ul>
        </>
      )}
      <h4>Evidence</h4>
      {answer.evidence.length === 0 ? (
        <p>No evidence returned.</p>
      ) : (
        <ul data-testid="evidence-list">
          {answer.evidence.map((e) => {
            const paymentId = PAYMENT_REF.exec(e.ref)?.[1];
            return (
              <li key={e.ref} data-testid="evidence-item">
                <code>{e.ref}</code> — {e.summary}
                {paymentId !== undefined && props.knownPaymentIds.has(paymentId) ? (
                  <>
                    {" "}
                    <button type="button" className="link" onClick={() => props.onOpenPayment(paymentId)}>
                      Open payment
                    </button>
                  </>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
      {answer.tool_calls === undefined || answer.tool_calls.length === 0 ? null : (
        <details>
          <summary>Tool calls ({answer.tool_calls.length})</summary>
          <ul>
            {answer.tool_calls.map((call, i) => (
              <li key={`${call.name}-${i}`}>
                <code>{call.name}</code>{" "}
                {Object.entries(call.args)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(", ")}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
