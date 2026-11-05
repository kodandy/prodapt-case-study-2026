/** Response shapes of the R2-8 analytics routes (docs/specs/analytics.md). */

export interface DailyPoint {
  day: string;
  amount_minor: number;
}

export interface ForecastPoint {
  day: string;
  expected_minor: number;
  low_minor: number;
  high_minor: number;
}

export interface Backtest {
  horizon_days: number;
  folds: number;
  mae_minor: number;
  wape: number | null;
  naive_wape: number | null;
}

export interface CurrencyForecast {
  currency: string;
  alpha: number;
  history: DailyPoint[];
  forecast: ForecastPoint[];
  backtest: Backtest | null;
}

export interface ForecastResponse {
  generated_at: string;
  horizon_days: number;
  history_days: number;
  currencies: CurrencyForecast[];
}

export interface ChargebackModel {
  features: string[];
  train_count: number;
  test_count: number;
  test_positives: number;
  auc: number | null;
  brier: number;
  base_rate_brier: number;
  trained_on: string;
}

export interface RiskItem {
  payment_id: string;
  amount_minor: number;
  currency: string;
  occurred_at: string;
  probability: number;
  contributions: Record<string, number>;
}

export interface ChargebackRiskResponse {
  captured_count: number;
  chargeback_count: number;
  merchant_chargeback_rate: number;
  model: ChargebackModel;
  items: RiskItem[];
}

export interface AnomalyAlert {
  alert_id: string;
  /** "failure_rate" | "volume" | "decline_mix"; kept open so a new server metric still renders. */
  metric: string;
  detail: string | null;
  bucket_start: string;
  observed: number;
  expected: number;
  zscore: number;
  attempts: number;
  created_at: string;
  updated_at: string;
}

export interface AlertsResponse {
  server_time: string;
  items: AnomalyAlert[];
}
