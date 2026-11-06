import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { errorMessageOf, useAuthState, useServices } from "../../app/services";

/** Body of POST /v1/assistant/query (ops-ai `ToolAnswer`, serialised with `asdict`). */
export interface ToolQueryAnswer {
  abstained: boolean;
  /** Narration mode: "llm" when the narration was kept, else "t2_fallback" (evidence only). */
  mode: string;
  text: string | null;
  /** Narration model ("mock" for the mock LLM), null when no narration was kept. */
  model: string | null;
  /** Tool planner: "rules" for the rule-based planner, else the LLM planner's model. */
  planner_model: string;
  claims: ToolQueryClaim[];
  tool_calls: ToolQueryCall[];
  rejected_calls: ToolQueryRejectedCall[];
  evidence: ToolQueryEvidence[];
}

export interface ToolQueryClaim {
  text: string;
  evidence_refs: string[];
}

export interface ToolQueryCall {
  name: string;
  args: Record<string, string>;
}

export interface ToolQueryRejectedCall {
  name: string;
  reason: string;
}

export interface ToolQueryEvidence {
  ref: string;
  summary: string;
}

const QUERY_PATH = "/v1/assistant/query";
const RULES_PLANNER = "rules";
const MOCK_MODEL = "mock";

function plannerLabel(plannerModel: string): string {
  return plannerModel === RULES_PLANNER ? "rule-based" : "LLM";
}

function narrationLabel(answer: ToolQueryAnswer): string {
  if (answer.mode !== "llm") return "evidence only";
  return answer.model === MOCK_MODEL ? "mock" : "LLM";
}

/** Plain text only (React escaping); nothing in the answer can trigger a request. */
function ToolQueryAnswerView(props: { answer: ToolQueryAnswer }): ReactNode {
  const { answer } = props;
  return (
    <div className="answer" data-testid="tool-query-answer">
      <dl>
        <dt>Planner</dt>
        <dd data-testid="tool-query-planner">
          <code>{answer.planner_model}</code> ({plannerLabel(answer.planner_model)})
        </dd>
        <dt>Narration</dt>
        <dd data-testid="tool-query-narration">
          <code>{answer.mode}</code>
          {answer.model === null ? null : (
            <>
              {" "}
              <code>{answer.model}</code>
            </>
          )}{" "}
          ({narrationLabel(answer)})
        </dd>
        <dt>Abstained</dt>
        <dd data-testid="tool-query-abstained">{answer.abstained ? "yes" : "no"}</dd>
      </dl>
      {answer.text === null ? null : (
        <p className="answer-text" data-testid="tool-query-text">
          {answer.text}
        </p>
      )}
      <h4>Tools called</h4>
      {answer.tool_calls.length === 0 ? (
        <p>No tools were called.</p>
      ) : (
        <ol data-testid="tool-query-calls">
          {answer.tool_calls.map((call, index) => (
            <li key={`${call.name}-${index}`} data-testid="tool-query-call">
              <code>{call.name}</code>{" "}
              <span data-testid="tool-query-call-args">
                {Object.entries(call.args)
                  .map(([key, value]) => `${key}=${value}`)
                  .join(", ")}
              </span>
            </li>
          ))}
        </ol>
      )}
      {answer.rejected_calls.length === 0 ? null : (
        <>
          <h4>Rejected tool calls</h4>
          <ul data-testid="tool-query-rejected">
            {answer.rejected_calls.map((call, index) => (
              <li key={`${call.name}-${index}`}>
                <code>{call.name}</code> {call.reason}
              </li>
            ))}
          </ul>
        </>
      )}
      {answer.claims.length === 0 ? null : (
        <>
          <h4>Claims</h4>
          <ul data-testid="tool-query-claims">
            {answer.claims.map((claim, index) => (
              <li key={`${claim.text}-${index}`}>
                {claim.text} [{claim.evidence_refs.join(", ")}]
              </li>
            ))}
          </ul>
        </>
      )}
      <h4>Evidence refs</h4>
      {answer.evidence.length === 0 ? (
        <p>No evidence returned.</p>
      ) : (
        <ul data-testid="tool-query-evidence">
          {answer.evidence.map((item) => (
            <li key={item.ref} data-testid="tool-query-evidence-item">
              <code>{item.ref}</code> — {item.summary}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Free-form question answered by the ops-ai tool planner. Only the latest request may update the view: a new
 * request, a merchant change and unmounting all make earlier answers stale.
 */
export function AssistantQueryPanel(): ReactNode {
  const { client } = useServices();
  const merchantApiKey = useAuthState().merchantApiKey;
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<ToolQueryAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const latest = useRef(0);
  const lastQuestion = useRef<string | null>(null);

  useEffect(() => {
    return () => {
      latest.current += 1;
      lastQuestion.current = null;
      setAnswer(null);
      setError(null);
      setPending(false);
    };
  }, [merchantApiKey]);

  async function run(text: string): Promise<void> {
    latest.current += 1;
    const ticket = latest.current;
    lastQuestion.current = text;
    setPending(true);
    setError(null);
    const result = await client.request<ToolQueryAnswer>({
      base: "ops",
      path: QUERY_PATH,
      method: "POST",
      auth: "merchantOps",
      body: { question: text },
    });
    if (ticket !== latest.current) return;
    setPending(false);
    if (result.ok) {
      setAnswer(result.data);
      return;
    }
    setAnswer(null);
    setError(errorMessageOf(result));
  }

  function submit(event: FormEvent): void {
    event.preventDefault();
    const text = question.trim();
    if (pending || text === "") return;
    void run(text);
  }

  function retry(): void {
    const text = lastQuestion.current;
    if (pending || text === null) return;
    void run(text);
  }

  return (
    <section aria-labelledby="tool-query-title">
      <h3 id="tool-query-title">Tool-calling query</h3>
      <p className="hint">Free-form; the planner picks read-only tools and shows what it called.</p>
      <form onSubmit={submit}>
        <label>
          Free-form prompt
          <textarea rows={3} maxLength={2000} value={question} onChange={(e) => setQuestion(e.target.value)} />
        </label>
        <button type="submit" disabled={pending}>
          {pending ? "Running…" : "Run tool query"}
        </button>
      </form>
      {error === null ? null : (
        <div>
          <p role="alert">{error}</p>
          <button type="button" onClick={retry} disabled={pending}>
            Retry
          </button>
        </div>
      )}
      {pending ? <p role="status">Running the tool planner…</p> : null}
      {answer === null || pending ? null : <ToolQueryAnswerView answer={answer} />}
    </section>
  );
}
