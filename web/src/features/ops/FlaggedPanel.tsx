import { useRef, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import { formatPercent, formatUtcMinute } from "../dashboard/format";
import type { CardTestingEpisode, FraudInvestigation } from "./types";

const CARD_TESTING_QUESTION = "Are there signs of card testing on my account?";

function EpisodeItem(props: { episode: CardTestingEpisode }): ReactNode {
  const { episode } = props;
  return (
    <li data-testid="episode" className="alert-item">
      <strong>{`${formatUtcMinute(episode.start)} – ${formatUtcMinute(episode.end)}`}</strong>
      <span>{` · ${episode.micro_payments} micro payments of ${episode.card_payments} card payments · declined ${formatPercent(episode.decline_ratio)}`}</span>
      {episode.card_bins.length === 0 ? null : <span className="hint">{` · BIN ${episode.card_bins.join(", ")}`}</span>}
      <span className="hint">{` · reasons: ${episode.reasons.join(", ")}`}</span>
    </li>
  );
}

export function FlaggedPanel(): ReactNode {
  const { client } = useServices();
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const [answer, setAnswer] = useState<FraudInvestigation | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function scan(): Promise<void> {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    const result = await client.request<FraudInvestigation>({
      base: "ops",
      path: "/v1/assistant/investigate-fraud",
      method: "POST",
      auth: "merchantOps",
      body: { question: CARD_TESTING_QUESTION },
    });
    inFlight.current = false;
    setPending(false);
    setError(errorMessageOf(result));
    setAnswer(result.ok ? result.data : null);
  }

  const flagged = (answer?.analysis?.episodes ?? []).filter((episode) => episode.flagged);

  return (
    <section aria-labelledby="flagged-title">
      <h2 id="flagged-title">Flagged transactions</h2>
      <button type="button" className="primary" disabled={pending} onClick={() => void scan()}>
        Scan for card testing
      </button>
      {error === null ? null : <p role="alert">{error}</p>}
      {answer === null ? null : (
        <div className="stack-tight">
          <p className="hint">{`Alerts: ${answer.alerts.length} raised by this scan`}</p>
          {flagged.length === 0 ? (
            <p className="muted">No flagged episodes.</p>
          ) : (
            <ul className="plain-list">
              {flagged.map((episode) => (
                <EpisodeItem key={episode.first_payment_id} episode={episode} />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}
