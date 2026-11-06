import type { Currency } from "../../lib/money";

/** The states GET /v1/payments returns and accepts as its `state` filter. */
export type ApiPaymentState = "created" | "succeeded" | "declined" | "failed" | "unknown";

export type PaymentState = ApiPaymentState | string;

export const PAYMENT_STATE_FILTERS: readonly { value: ApiPaymentState; label: string }[] = [
  { value: "created", label: "Created" },
  { value: "succeeded", label: "Succeeded" },
  { value: "declined", label: "Declined" },
  { value: "failed", label: "Failed" },
  { value: "unknown", label: "Unknown" },
];

/** Only settled card payments can be refunded; the server still makes the final decision. */
const REFUNDABLE_STATES: ReadonlySet<string> = new Set(["succeeded"]);

export function canRefund(state: PaymentState): boolean {
  return REFUNDABLE_STATES.has(state);
}

export interface PaymentSummary {
  payment_id: string;
  state: PaymentState;
  amount_minor: number;
  currency: Currency;
  method: string;
  card: { bin: string; last4: string } | null;
  decline_code: string | null;
  refunded_minor: number;
  disputed_minor: number;
  chargeback_minor: number;
  created_at: string;
}

export interface PaymentList {
  items: PaymentSummary[];
  next_cursor: string | null;
}

export interface PaymentRisk {
  payment_id: string;
  risk_flagged: boolean;
  blocked: boolean;
}

export interface RefundResult {
  refund_id: string;
  payment_id: string;
  state: string;
  amount_minor: number;
  currency: Currency;
  payment_refunded_minor: number;
  payment_refundable_minor: number;
}

export interface MerchantTokenResponse {
  access_token: string;
  expires_in: number;
}

export interface Evidence {
  ref: string;
  summary: string;
}

export interface ToolCall {
  name: string;
  args: Record<string, string>;
}

/** The common part of /ask, /investigate and /investigate-fraud answers. */
export interface AssistantAnswer {
  mode: "llm" | "t2_fallback";
  text: string | null;
  model: string | null;
  evidence: Evidence[];
  suggested_fixes?: string[];
  tool_calls?: ToolCall[];
}

export interface SettlementLine {
  kind: string;
  amount_minor: number;
  count: number;
}

export interface SettlementExplanation extends AssistantAnswer {
  period: string;
  currency: Currency;
  contract_version: string;
  fx_rate_id: string;
  lines: SettlementLine[];
  gross_minor: number;
  expected_minor: number;
  net_minor: number;
  gap_minor: number;
  verified: boolean;
  mismatches: string[];
  summary: string;
}

export interface DisputeView {
  dispute_id: string;
  payment_id: string;
  state: string;
  amount_minor: number;
  currency: Currency;
  reason: string;
  chargeback_fee_minor: number;
  created_at: string;
  updated_at: string;
}

export interface DisputeList {
  items: DisputeView[];
  next_cursor: string | null;
}

export function refundableMinor(p: PaymentSummary): number {
  if (!canRefund(p.state)) return 0;
  return Math.max(0, p.amount_minor - p.refunded_minor - p.disputed_minor - p.chargeback_minor);
}
