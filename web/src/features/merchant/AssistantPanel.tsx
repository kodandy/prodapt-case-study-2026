import { useState, type FormEvent, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { AnswerView } from "./AnswerView";
import { AssistantQueryPanel } from "./AssistantQueryPanel";
import type { AssistantAnswer } from "./types";

const MODES = [
  { path: "/v1/assistant/investigate", label: "Investigate failures" },
  { path: "/v1/assistant/investigate-fraud", label: "Investigate fraud" },
  { path: "/v1/assistant/ask", label: "Ask (search)" },
] as const;

type ModePath = (typeof MODES)[number]["path"];
const ASK_PATH: ModePath = "/v1/assistant/ask";

export function AssistantPanel(props: {
  knownPaymentIds: ReadonlySet<string>;
  onOpenPayment: (paymentId: string) => void;
}): ReactNode {
  const { client } = useServices();
  const [mode, setMode] = useState<ModePath>("/v1/assistant/investigate");
  const [question, setQuestion] = useState("");
  const [grounded, setGrounded] = useState(false);
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (pending || question.trim() === "") return;
    setPending(true);
    setMessage(null);
    const result = await client.request<AssistantAnswer>({
      base: "ops",
      path: mode,
      method: "POST",
      auth: "merchantOps",
      query: { grounded: mode === ASK_PATH && grounded ? "true" : undefined },
      body: { question: question.trim() },
    });
    setPending(false);
    if (result.ok) setAnswer(result.data);
    else setMessage(errorMessageOf(result));
  }

  return (
    <section aria-labelledby="assistant-title">
      <h2 id="assistant-title">Assistant</h2>
      <form onSubmit={(e) => void submit(e)}>
        <label>
          Mode
          <select value={mode} onChange={(e) => setMode(e.target.value as ModePath)}>
            {MODES.map((m) => (
              <option key={m.path} value={m.path}>
                {m.label}
              </option>
            ))}
          </select>
        </label>
        {mode === ASK_PATH ? (
          <label>
            <input type="checkbox" checked={grounded} onChange={(e) => setGrounded(e.target.checked)} /> Grounded
            citations
          </label>
        ) : null}
        <label>
          Question
          <textarea rows={3} maxLength={2000} value={question} onChange={(e) => setQuestion(e.target.value)} />
        </label>
        <button type="submit" disabled={pending}>
          {pending ? "Working…" : "Ask"}
        </button>
      </form>
      {message === null ? null : <p role="alert">{message}</p>}
      {answer === null ? null : (
        <AnswerView answer={answer} knownPaymentIds={props.knownPaymentIds} onOpenPayment={props.onOpenPayment} />
      )}
      <AssistantQueryPanel />
    </section>
  );
}
