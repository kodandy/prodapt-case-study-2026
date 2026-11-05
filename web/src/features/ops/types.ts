import type { AssistantAnswer } from "../merchant/types";

export interface FailureWindow {
  start: string;
  end: string;
  total: number;
  failed: number;
  rate: number;
}

/** POST /v1/assistant/investigate. */
export interface FailureInvestigation extends AssistantAnswer {
  intent: string;
  cause_label: string;
  window: FailureWindow | null;
}

export interface CardTestingEpisode {
  first_payment_id: string;
  start: string;
  end: string;
  payment_ids: string[];
  micro_payments: number;
  card_payments: number;
  micro_share: number;
  distinct_cards: number | null;
  declined: number;
  decline_ratio: number;
  card_bins: string[];
  currencies: string[];
  flagged: boolean;
  reasons: string[];
}

export interface CardTestingAlert {
  alert_id: string;
  ticket_id: string | null;
  queue: string;
  first_payment_id: string;
}

/** POST /v1/assistant/investigate-fraud. */
export interface FraudInvestigation extends AssistantAnswer {
  intent: string;
  cause_label: string;
  analysis: { start: string; end: string; micro_payments: number; episodes: CardTestingEpisode[] } | null;
  alerts: CardTestingAlert[];
}

export type TicketStatus = "open" | "assigned" | "resolved";

export interface Ticket {
  ticket_id: string;
  source: string;
  queue: string;
  priority: string;
  status: TicketStatus | string;
  title: string;
  reason: string;
  assignee: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface TicketList {
  tickets: Ticket[];
}
