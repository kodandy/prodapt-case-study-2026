// P1-5 load test: POST /v1/payments at a constant arrival rate for 90s (under the 2-minute budget).
// Target (NFR-PERF): p99 < 2s, error rate < 1%. A decline (402) is a correct answer, not an error.
// Run with `make perf`; the summary is written to docs/perf/k6-summary.json.
import http from "k6/http";
import { check } from "k6";

const GATEWAY_URL = __ENV.GATEWAY_URL || "http://gateway-api:8000";
const MERCHANT_KEY = __ENV.MERCHANT_KEY;
const RATE = Number(__ENV.RATE || 40);

// Published test PANs only; never real card data.
const PANS = ["4242424242424242", "5555555555554444", "4000000000000119"];

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 299 }, 402));

export const options = {
  scenarios: {
    payments: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: "90s",
      preAllocatedVUs: 50,
      maxVUs: 200,
    },
  },
  thresholds: {
    "http_req_duration{name:POST /v1/payments}": ["p(99)<2000"],
    http_req_failed: ["rate<0.01"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

export function setup() {
  if (!MERCHANT_KEY) {
    throw new Error("MERCHANT_KEY is not set (make perf issues one)");
  }
}

export default function () {
  const pan = PANS[Math.floor(Math.random() * PANS.length)];
  const res = http.post(
    `${GATEWAY_URL}/v1/payments`,
    JSON.stringify({
      amount_minor: 100 + Math.floor(Math.random() * 9900),
      currency: "USD",
      card: { number: pan, exp_month: 12, exp_year: 2030, cvc: "123" },
    }),
    {
      headers: {
        Authorization: `Bearer ${MERCHANT_KEY}`,
        "Idempotency-Key": crypto.randomUUID(),
        "Content-Type": "application/json",
      },
      tags: { name: "POST /v1/payments" },
    },
  );
  check(res, { "answered (2xx or 402)": (r) => (r.status >= 200 && r.status < 300) || r.status === 402 });
}

export function handleSummary(data) {
  const d = data.metrics["http_req_duration{name:POST /v1/payments}"].values;
  const failed = data.metrics.http_req_failed.values.rate;
  const line =
    `POST /v1/payments: p50=${d.med.toFixed(1)}ms p95=${d["p(95)"].toFixed(1)}ms ` +
    `p99=${d["p(99)"].toFixed(1)}ms max=${d.max.toFixed(1)}ms error_rate=${(failed * 100).toFixed(2)}% ` +
    `requests=${data.metrics.http_reqs.values.count}\n`;
  return { stdout: line, "/results/k6-summary.json": JSON.stringify(data, null, 2) };
}
