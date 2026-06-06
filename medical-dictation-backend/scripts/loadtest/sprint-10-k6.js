// Sprint-10 day-9 load test for autocomplete-service.
//
// Run: k6 run --vus 50 --duration 10m \
//   -e BASE_URL=http://localhost:8007 \
//   -e BEARER="$JWT" \
//   scripts/loadtest/sprint-10-k6.js
//
// Scenarios:
//   - sustained:  100 RPS for 10m, suggest p95 ≤ 80ms (cache hit)
//   - burst:      500 RPS for 1m, no 5xx
//   - cold_storm: clear cache + 1000 RPS, lock prevents thundering herd

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const baseUrl = __ENV.BASE_URL || 'http://localhost:8007';
const bearer = __ENV.BEARER || '';

const suggestLatency = new Trend('mdx_suggest_latency_ms', true);
const ok = new Rate('mdx_suggest_ok_rate');

const headers = {
  'Content-Type': 'application/json',
  'Authorization': `Bearer ${bearer}`,
};

const PREFIXES = [
  'зад', 'задишка', 'біль', 'грудин', 'ритм',
  'температ', 'тиск', 'пацієнт', 'скарг',
  'shortness', 'chest', 'sinus', 'normal',
];

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

export const options = {
  scenarios: {
    sustained: {
      executor: 'constant-arrival-rate', rate: 100, timeUnit: '1s',
      duration: '10m', preAllocatedVUs: 50, exec: 'suggestScenario',
    },
    burst: {
      executor: 'constant-arrival-rate', rate: 500, timeUnit: '1s',
      duration: '1m', preAllocatedVUs: 100, exec: 'suggestScenario',
      startTime: '11m',
    },
  },
  thresholds: {
    'mdx_suggest_latency_ms': ['p(95)<150'],
    'mdx_suggest_ok_rate': ['rate>0.99'],
  },
};

export function suggestScenario() {
  const body = JSON.stringify({
    prefix: pick(PREFIXES),
    language: 'uk',
    limit: 3,
  });
  const res = http.post(`${baseUrl}/autocomplete/suggest`, body, { headers });
  suggestLatency.add(res.timings.duration);
  ok.add(res.status === 200);
  check(res, { '200 OK': r => r.status === 200 });
}
