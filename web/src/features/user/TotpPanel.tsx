import { useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import type { TotpEnrolResponse } from "./types";

/** Enrolment shows the otpauth URI and secret as text (no QR library); verifying ends the pre-TOTP session. */
export function TotpPanel(props: { onEnabled: () => void }): ReactNode {
  const { client } = useServices();
  const [enrolment, setEnrolment] = useState<TotpEnrolResponse | null>(null);
  const [code, setCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function enrol(): Promise<void> {
    setPending(true);
    const result = await client.request<TotpEnrolResponse>({
      base: "gw",
      path: "/v1/me/mfa/totp",
      method: "POST",
      auth: "user",
    });
    setPending(false);
    if (result.ok) setEnrolment(result.data);
    else setMessage(errorMessageOf(result));
  }

  async function verify(): Promise<void> {
    setPending(true);
    const result = await client.request({
      base: "gw",
      path: "/v1/me/mfa/totp/verify",
      method: "POST",
      auth: "user",
      body: { code },
    });
    setPending(false);
    if (result.ok) {
      setEnrolment(null);
      props.onEnabled();
    } else {
      setMessage(errorMessageOf(result));
    }
  }

  return (
    <section aria-labelledby="totp-title">
      <h2 id="totp-title">Two-factor authentication</h2>
      {enrolment === null ? (
        <button type="button" onClick={() => void enrol()} disabled={pending}>
          Set up an authenticator app
        </button>
      ) : (
        <div>
          <p>Add this account to your authenticator app, then enter the 6-digit code.</p>
          <dl>
            <dt>Secret</dt>
            <dd>
              <code data-testid="totp-secret">{enrolment.secret}</code>
            </dd>
            <dt>otpauth URI</dt>
            <dd>
              <code>{enrolment.otpauth_uri}</code>
            </dd>
          </dl>
          <label>
            Code
            <input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} />
          </label>
          <button type="button" onClick={() => void verify()} disabled={pending}>
            Turn on
          </button>
        </div>
      )}
      {message === null ? null : <p role="status">{message}</p>}
    </section>
  );
}
