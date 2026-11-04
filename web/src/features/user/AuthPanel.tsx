import { useState, type FormEvent, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import type { SessionResponse } from "./types";

type Mode = "login" | "signup";

export function AuthPanel(props: { notice: string | null }): ReactNode {
  const { client, store } = useServices();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [needsTotp, setNeedsTotp] = useState(false);
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(props.notice);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (pending) return;
    setPending(true);
    setMessage(null);
    try {
      if (mode === "signup") {
        const result = await client.request({
          base: "gw",
          path: "/v1/signup",
          method: "POST",
          auth: "none",
          body: { email, password },
        });
        if (result.ok) {
          setMode("login");
          setMessage("Account created. Log in.");
        } else {
          setMessage(errorMessageOf(result));
        }
        return;
      }
      const body = needsTotp ? { email, password, totp_code: totpCode } : { email, password };
      const result = await client.request<SessionResponse>({
        base: "gw",
        path: "/v1/sessions",
        method: "POST",
        auth: "none",
        body,
      });
      if (result.ok) {
        setPassword("");
        setTotpCode("");
        store.setUserSession(result.data.access_token);
      } else if (result.code === "mfa required") {
        setNeedsTotp(true);
        setMessage("Enter the code from your authenticator app.");
      } else {
        setMessage(errorMessageOf(result));
      }
    } finally {
      setPending(false);
    }
  }

  return (
    <section aria-labelledby="auth-title">
      <h2 id="auth-title">{mode === "login" ? "Log in" : "Sign up"}</h2>
      <form onSubmit={(e) => void submit(e)}>
        <label>
          Email
          <input type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label>
          Password
          <input
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {mode === "login" && needsTotp ? (
          <label>
            Authenticator code
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              value={totpCode}
              onChange={(e) => setTotpCode(e.target.value)}
            />
          </label>
        ) : null}
        <button type="submit" disabled={pending}>
          {mode === "login" ? "Log in" : "Create account"}
        </button>
      </form>
      {message === null ? null : <p role="status">{message}</p>}
      <button
        type="button"
        className="link"
        onClick={() => {
          setMode(mode === "login" ? "signup" : "login");
          setNeedsTotp(false);
          setMessage(null);
        }}
      >
        {mode === "login" ? "Create an account" : "I already have an account"}
      </button>
    </section>
  );
}
