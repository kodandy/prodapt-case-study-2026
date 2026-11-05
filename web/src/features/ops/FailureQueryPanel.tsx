import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { formatPercent } from "../dashboard/format";
import type { FailureInvestigation } from "./types";

function InvestigationView(props: { answer: FailureInvestigation }): ReactNode {
  const { answer } = props;
  const fixes = answer.suggested_fixes ?? [];
  const toolCalls = answer.tool_calls ?? [];
  return (
    <div className="stack-tight">
      <p>{`Cause: ${answer.cause_label}`}</p>
      {answer.mode === "t2_fallback" ? (
        <p className="hint">LLM unavailable: evidence only</p>
      ) : answer.text === null ? null : (
        <p className="answer-text">{answer.text}</p>
      )}
      {answer.window === null ? null : (
        <p>{`Window failure rate ${formatPercent(answer.window.rate)} (${answer.window.failed} of ${answer.window.total})`}</p>
      )}
      {fixes.length === 0 ? null : (
        <>
          <h3>Suggested fixes</h3>
          <ul>
            {fixes.map((fix) => (
              <li key={fix}>{fix}</li>
            ))}
          </ul>
        </>
      )}
      <h3>Evidence</h3>
      <ul>
        {answer.evidence.map((item) => (
          <li key={item.ref}>
            <code>{item.ref}</code> {item.summary}
          </li>
        ))}
      </ul>
      {toolCalls.length === 0 ? null : (
        <>
          <h3>Tool calls</h3>
          <ul>
            {toolCalls.map((call, index) => (
              <li key={`${call.name}-${index}`}>
                <code>{call.name}</code>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export function FailureQueryPanel(): ReactNode {
  const { client } = useServices();
  const [question, setQuestion] = useState("");
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const [answer, setAnswer] = useState<FailureInvestigation | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    const text = question.trim();
    if (inFlight.current || text === "") return;
    inFlight.current = true;
    setPending(true);
    const result = await client.request<FailureInvestigation>({
      base: "ops",
      path: "/v1/assistant/investigate",
      method: "POST",
      auth: "merchantOps",
      body: { question: text },
    });
    inFlight.current = false;
    setPending(false);
    setError(errorMessageOf(result));
    setAnswer(result.ok ? result.data : null);
  }

  return (
    <section aria-labelledby="failure-query-title">
      <h2 id="failure-query-title">Failure query</h2>
      <form onSubmit={(e) => void submit(e)}>
        <label>
          Question
          <textarea rows={3} value={question} onChange={(e) => setQuestion(e.target.value)} />
        </label>
        <button type="submit" disabled={pending}>
          Investigate
        </button>
      </form>
      {error === null ? null : <p role="alert">{error}</p>}
      {answer === null ? null : <InvestigationView answer={answer} />}
    </section>
  );
}
