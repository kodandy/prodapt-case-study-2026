import { useRef, useState, type FormEvent, type ReactNode } from "react";
import { messageFor } from "../../lib/errors";
import { useOpsServices } from "./opsServices";

/**
 * Ops staff login against the gateway (POST /v1/ops/sessions): email, password and the TOTP code in one step;
 * the code is mandatory. The password and code are cleared after every attempt.
 */
export function OpsSignIn(): ReactNode {
  const { client } = useOpsServices();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const [message, setMessage] = useState<string | null>(null);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    let failure: string | null = null;
    try {
      const outcome = await client.signIn({ email, password, totpCode: code });
      if (!outcome.ok) failure = outcome.code;
    } catch {
      failure = "network_error";
    } finally {
      inFlight.current = false;
    }
    if (failure !== null) {
      setPending(false);
      setPassword("");
      setCode("");
      setMessage(messageFor(failure));
    }
  }

  return (
    <section aria-labelledby="ops-sign-in-title">
      <h2 id="ops-sign-in-title">Ops staff sign-in</h2>
      <p className="hint">Cross-merchant view for ops staff. Password and authenticator code are both required.</p>
      <form onSubmit={(e) => void submit(e)}>
        <label>
          Email
          <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label>
          Authenticator code
          <input
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
        </label>
        <button type="submit" disabled={pending}>
          Sign in as ops staff
        </button>
      </form>
      {message === null ? null : <p role="alert">{message}</p>}
    </section>
  );
}
