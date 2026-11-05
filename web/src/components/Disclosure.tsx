import { useState, type ReactNode } from "react";

/** Mounts its content only after the reader opens it, so a panel that loads data does not fetch on page load. */
export function Disclosure({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        {open ? `Hide ${label}` : `Show ${label}`}
      </button>
      {open ? children : null}
    </div>
  );
}
