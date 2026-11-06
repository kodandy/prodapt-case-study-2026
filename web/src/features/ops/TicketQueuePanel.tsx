import { useEffect, useState, type ReactNode } from "react";
import { errorMessageOf, useServices } from "../../app/services";
import type { ApiResult } from "../../lib/api";
import { formatUtcMinute } from "../dashboard/format";
import type { Ticket, TicketList, TicketStatus } from "./types";
import { TableScroll } from "../../components/TableScroll";

const TICKET_LIMIT = 50;
/** Until ops roles exist the console acts with the merchant token, so it signs assignments as itself. */
const CONSOLE_ASSIGNEE = "ops-console";
const STATUS_OPTIONS: readonly ("" | TicketStatus)[] = ["", "open", "assigned", "resolved"];

type Transition = { status: "assigned"; assignee: string } | { status: "resolved" };

function transitionError(result: ApiResult<Ticket>): string | null {
  if (result.ok) return null;
  if (result.status === 403) return "Only this merchant's own escalations can be changed here.";
  if (result.status === 409) return "That ticket already moved on. Refresh the queue.";
  return errorMessageOf(result);
}

function TicketRow(props: { ticket: Ticket; busy: boolean; onMove: (move: Transition) => void }): ReactNode {
  const { ticket, busy, onMove } = props;
  return (
    <tr data-testid="ticket-row">
      <td>{ticket.title}</td>
      <td>{ticket.queue}</td>
      <td>{ticket.priority}</td>
      <td>{ticket.status}</td>
      <td>{ticket.assignee ?? "—"}</td>
      <td>{formatUtcMinute(ticket.created_at)}</td>
      <td>
        <div className="row-actions">
          {ticket.status === "open" ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => onMove({ status: "assigned", assignee: CONSOLE_ASSIGNEE })}
            >
              Assign to me
            </button>
          ) : null}
          {ticket.status === "open" || ticket.status === "assigned" ? (
            <button type="button" disabled={busy} onClick={() => onMove({ status: "resolved" })}>
              Resolve
            </button>
          ) : null}
        </div>
      </td>
    </tr>
  );
}

export function TicketQueuePanel(): ReactNode {
  const { client } = useServices();
  const [statusFilter, setStatusFilter] = useState<"" | TicketStatus>("");
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void client
      .request<TicketList>({
        base: "ops",
        path: "/v1/tickets",
        auth: "merchantOps",
        query: { limit: TICKET_LIMIT, status: statusFilter === "" ? undefined : statusFilter },
      })
      .then((result) => {
        if (!active) return;
        setError(errorMessageOf(result));
        setTickets(result.ok ? result.data.tickets : null);
      });
    return () => {
      active = false;
    };
  }, [client, statusFilter]);

  async function move(ticketId: string, transition: Transition): Promise<void> {
    if (busyId !== null) return;
    setBusyId(ticketId);
    const result = await client.request<Ticket>({
      base: "ops",
      path: `/v1/tickets/${encodeURIComponent(ticketId)}/transition`,
      method: "POST",
      auth: "merchantOps",
      body: transition,
    });
    setBusyId(null);
    setError(transitionError(result));
    if (!result.ok) return;
    const updated = result.data;
    setTickets((previous) => (previous ?? []).map((t) => (t.ticket_id === updated.ticket_id ? updated : t)));
  }

  return (
    <section aria-labelledby="tickets-title">
      <h2 id="tickets-title">Ticket queue</h2>
      <label className="inline-field">
        Status
        <select value={statusFilter} onChange={(e) => setStatusFilter(STATUS_OPTIONS.find((o) => o === e.target.value) ?? "")}>
          {STATUS_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option === "" ? "All" : option}
            </option>
          ))}
        </select>
      </label>
      {error === null ? null : <p role="alert">{error}</p>}
      {tickets === null ? null : tickets.length === 0 ? (
        <p className="muted">No tickets.</p>
      ) : (
        <TableScroll label="Ticket queue table">
          <table>
            <caption className="sr-only">Tickets</caption>
            <thead>
              <tr>
                <th scope="col">Title</th>
                <th scope="col">Queue</th>
                <th scope="col">Priority</th>
                <th scope="col">Status</th>
                <th scope="col">Assignee</th>
                <th scope="col">Created</th>
                <th scope="col">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {tickets.map((ticket) => (
                <TicketRow
                  key={ticket.ticket_id}
                  ticket={ticket}
                  busy={busyId !== null}
                  onMove={(transition) => void move(ticket.ticket_id, transition)}
                />
              ))}
            </tbody>
          </table>
        </TableScroll>
      )}
    </section>
  );
}
