import type { Currency } from "../../lib/money";

export type PaymentState =
  | "authorized"
  | "captured"
  | "declined"
  | "failed"
  | "refunded"
  | "partially_refunded"
  | "disputed"
  | string;

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

export function refundableMinor(p: PaymentSummary): number {
  return Math.max(0, p.amount_minor - p.refunded_minor - p.disputed_minor - p.chargeback_minor);
}
