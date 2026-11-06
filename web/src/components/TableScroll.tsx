import type { ReactNode } from "react";

/** Lets a wide table scroll sideways on a narrow screen; focusable so the keyboard can scroll it too. */
export function TableScroll(props: { label: string; children: ReactNode }): ReactNode {
  return (
    <div className="table-scroll" role="region" aria-label={props.label} tabIndex={0}>
      {props.children}
    </div>
  );
}
