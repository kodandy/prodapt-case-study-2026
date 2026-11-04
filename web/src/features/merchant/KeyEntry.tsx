import { useState, type FormEvent, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import type { MerchantTokenResponse } from "./types";

/** Demo only (docs/specs/web.md decision 5): the key stays in memory and is exchanged for the ops-ai token. */
export function KeyEntry(): ReactNode {
  const { client, store } = useServices();
  const [apiKey, setApiKey] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    const key = apiKey.trim();
    if (pending || key === "") return;
    setPending(true);
    store.setMerchant(key, "");
    const result = await client.request<MerchantTokenResponse>({
      base: "gw",
      path: "/v1/merchant-tokens",
      method: "POST",
      auth: "merchantKey",
    });
    setPending(false);
    setApiKey("");
    if (result.ok) {
      store.setMerchantOpsToken(result.data.access_token);
      setMessage(null);
    } else {
      store.clearMerchant();
      setMessage(errorMessageOf(result));
    }
  }

  return (
    <section aria-labelledby="key-title">
      <h2 id="key-title">Merchant sign-in (demo)</h2>
      <p className="hint">
        Local demo only: paste the key printed by <code>make web-merchant-key</code>. It is kept in memory for this tab and never stored.
      </p>
      <form onSubmit={(e) => void submit(e)}>
        <label>
          API key
          <input type="password" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
        </label>
        <button type="submit" disabled={pending}>
          Continue
        </button>
      </form>
      {message === null ? null : <p role="alert">{message}</p>}
    </section>
  );
}
