import type { Currency } from "../../lib/money";

export interface SessionResponse {
  access_token: string;
  expires_in: number;
  amr: string[];
}

export interface TotpEnrolResponse {
  secret: string;
  otpauth_uri: string;
}

export interface WalletBalance {
  account_id: string;
  currency: Currency;
  available_minor: number;
  held_minor: number;
}

export interface WalletList {
  items: WalletBalance[];
}

export interface TransferResult {
  transfer_id: string;
  state: string;
  amount_minor: number;
  currency: Currency;
  sender_available_minor: number;
}

export interface TransferHistoryItem {
  transfer_id: string;
  direction: "sent" | "received";
  counterparty_email_masked: string;
  amount_minor: number;
  currency: Currency;
  created_at: string;
}

export interface TransferHistory {
  items: TransferHistoryItem[];
  next_cursor: string | null;
}
