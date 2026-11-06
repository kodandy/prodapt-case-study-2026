import type { ReactNode } from "react";
import type { AssistantAnswer } from "./types";

/** R1-5 grounded /ask: each kept sentence with its sources' verbatim excerpts, and the dropped sentences. */
export function GroundedCitations(props: { answer: AssistantAnswer }): ReactNode {
  const { grounded, citations = [], unsupported_sentences: unsupported = [] } = props.answer;
  return (
    <div data-testid="grounded-citations">
      <p className="muted">{grounded === true ? "Grounded in the sources below." : "Not grounded in any source."}</p>
      {citations.length === 0 ? null : (
        <ol data-testid="cited-sentences">
          {citations.map((cited, i) => (
            <li key={`${i}-${cited.sentence}`}>
              {cited.sentence}
              <ul>
                {cited.sources.map((source) => (
                  <li key={source.ref}>
                    <code>{source.ref}</code>: <q>{source.excerpt}</q>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ol>
      )}
      {unsupported.length === 0 ? null : (
        <details>
          <summary>Dropped sentences ({unsupported.length})</summary>
          <ul data-testid="unsupported-sentences">
            {unsupported.map((u, i) => (
              <li key={`${i}-${u.sentence}`}>
                {u.reason === "unknown_ref" ? `Cites a source not retrieved (${u.refs.join(", ")})` : "No citation"}:{" "}
                {u.sentence}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
