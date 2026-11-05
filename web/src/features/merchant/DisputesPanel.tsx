import { useCallback, useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { CURRENCIES, formatMinor } from "../../lib/money";
import type { DisputeList, DisputeView } from "./types";

const PAGE_SIZE = 20;
const MAX_EVIDENCE_LENGTH = 4000;

const ERROR_MESSAGES: ReadonlyMap<string, string> = new Map([
  ["invalid dispute transition", "Evidence can only be added while the dispute is opened."],
  ["dispute not found", "Dispute not found."],
]);

type Feedback = { kind: "status" | "alert"; text: string } | null;

function amountText(dispute: DisputeView): string {
  return (CURRENCIES as readonly string[]).includes(dispute.currency)
    ? formatMinor(dispute.amount_minor, dispute.currency)
    : `${dispute.amount_minor} ${dispute.currency}`;
}

/** B13.2: the merchant's disputes; an opened dispute takes one evidence text, which moves it to under_review. */
export function DisputesPanel(): ReactNode {
  const { client } = useServices();
  const [items, setItems] = useState<DisputeView[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<Feedback>(null);

  const load = useCallback(
    async (after: string | null): Promise<void> => {
      const result = await client.request<DisputeList>({
        base: "gw",
        path: "/v1/disputes",
        auth: "merchantKey",
        query: { limit: PAGE_SIZE, cursor: after ?? undefined },
      });
      setLoadError(errorMessageOf(result));
      if (!result.ok) return;
      setItems((prev) => (after === null || prev === null ? result.data.items : [...prev, ...result.data.items]));
      setCursor(result.data.next_cursor);
    },
    [client],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  function replace(next: DisputeView): void {
    setItems((prev) => (prev === null ? prev : prev.map((d) => (d.dispute_id === next.dispute_id ? next : d))));
  }

  return (
    <section aria-labelledby="disputes-title">
      <h2 id="disputes-title">Disputes</h2>
      {loadError === null ? null : <p role="alert">{loadError}</p>}
      {items === null ? null : items.length === 0 ? (
        <p className="muted">No disputes.</p>
      ) : (
        <ul className="list">
          {items.map((dispute) => (
            <li key={dispute.dispute_id}>
              <span>{amountText(dispute)}</span> <span className="muted">{dispute.reason}</span>{" "}
              <span className="badge">{dispute.state}</span>
              <div className="muted">Payment {dispute.payment_id}</div>
              {dispute.state === "opened" ? (
                <EvidenceForm dispute={dispute} onSubmitted={replace} onFeedback={setFeedback} />
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {cursor === null ? null : (
        <button type="button" onClick={() => void load(cursor)}>
          Load more
        </button>
      )}
      {feedback === null ? null : <p role={feedback.kind}>{feedback.text}</p>}
    </section>
  );
}

function EvidenceForm(props: {
  dispute: DisputeView;
  onSubmitted: (next: DisputeView) => void;
  onFeedback: (feedback: Feedback) => void;
}): ReactNode {
  const { client } = useServices();
  const { dispute, onSubmitted, onFeedback } = props;
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);

  async function submit(): Promise<void> {
    if (text.trim() === "") {
      onFeedback({ kind: "alert", text: "Enter the evidence before submitting." });
      return;
    }
    setSending(true);
    const result = await client.request<DisputeView>({
      base: "gw",
      path: `/v1/disputes/${encodeURIComponent(dispute.dispute_id)}/evidence`,
      method: "POST",
      auth: "merchantKey",
      body: { text },
    });
    setSending(false);
    if (result.ok) {
      onFeedback({ kind: "status", text: "Evidence submitted." });
      onSubmitted(result.data);
      return;
    }
    onFeedback({ kind: "alert", text: ERROR_MESSAGES.get(result.code) ?? errorMessageOf(result) ?? "" });
  }

  return (
    <div className="form">
      <label>
        Evidence
        <textarea maxLength={MAX_EVIDENCE_LENGTH} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <button type="button" disabled={sending} onClick={() => void submit()}>
        Submit evidence
      </button>
    </div>
  );
}
