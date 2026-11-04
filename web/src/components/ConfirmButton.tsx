import { useRef, useState, type ReactNode } from "react";

export interface ConfirmButtonProps {
  onConfirm: () => Promise<void>;
  children: ReactNode;
  disabled?: boolean;
}

/** Disabled while onConfirm is pending, so a double click sends one request (decision 8). */
export function ConfirmButton(props: ConfirmButtonProps): ReactNode {
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);

  async function click(): Promise<void> {
    if (inFlight.current || props.disabled === true) return;
    inFlight.current = true;
    setPending(true);
    try {
      await props.onConfirm();
    } catch {
      // The caller reports its own failure; the button only has to become usable again.
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }

  return (
    <button type="button" className="primary" disabled={pending || props.disabled === true} onClick={() => void click()}>
      {props.children}
    </button>
  );
}
