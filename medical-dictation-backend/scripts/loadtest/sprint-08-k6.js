// Sprint-08 day-9 load test scenarios for k6.
//
// Run: k6 run --vus 200 --duration 5m -e BASE_URL=https://api.dev.example \
//             -e BEARER=<jwt> scripts/loadtest/sprint-08-k6.js
//
// Scenarios (selected at random per VU):
//   - autosave   : PUT /v1/reports/{id}/draft 1× per 5s, 200 VUs sustained
//   - finalize   : POST /v1/reports/{id}/finalize (15% of completed drafts)
//   - search     : GET  /v1/reports/search with random filter combos
//   - diff       : GET  /v1/reports/{id}/diff?from=1&to=N
//
// Targets (spec §6):
//   - p95 search ≤ 250ms with q, ≤ 100ms filter-only
//   - p95 diff   ≤ 150ms cold, ≤ 5ms cached
//   - p95 autosave ≤ 200ms
//   - 409 rate on burst-finalize == 49/50

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';

const baseUrl = __ENV.BASE_URL || 'http://localhost:8004';
const bearer = __ENV.BEARER || '';
const reportIds = JSON.parse(open(__ENV.REPORT_IDS_JSON || './report-ids.json'));

const searchLatency = new Trend('mdx_search_latency_ms', true);
const diffLatency   = new Trend('mdx_diff_latency_ms', true);
const autosaveLatency = new Trend('mdx_autosave_latency_ms', true);
const autosaveConflict = new Counter('mdx_autosave_conflicts');
const autosaveOk = new Rate('mdx_autosave_ok_rate');

const headers = {
  'Content-Type': 'application/json',
  Authorization: `Bearer ${bearer}`,
  'X-Read-Purpose': 'qa_review',
};

function rand(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

export const options = {
  scenarios: {
    autosave: {
      executor: 'constant-vus', vus: 100, duration: '5m', exec: 'autosaveScenario',
    },
    search: {
      executor: 'constant-arrival-rate', rate: 100, timeUnit: '1s',
      duration: '5m', preAllocatedVUs: 50, exec: 'searchScenario',
    },
    diff: {
      executor: 'constant-arrival-rate', rate: 50, timeUnit: '1s',
      duration: '5m', preAllocatedVUs: 20, exec: 'diffScenario',
    },
  },
  thresholds: {
    'mdx_search_latency_ms{has_q:true}': ['p(95)<250'],
    'mdx_search_latency_ms{has_q:false}': ['p(95)<100'],
    'mdx_diff_latency_ms': ['p(95)<150'],
    'mdx_autosave_latency_ms': ['p(95)<200'],
    'mdx_autosave_ok_rate': ['rate>0.95'],
  },
};

export function autosaveScenario() {
  const id = rand(reportIds);
  const body = JSON.stringify({
    expected_version: 1,
    content: {
      template_id: '00000000-0000-0000-0000-000000000000',
      template_schema_version: 1,
      title: `loadtest ${id}`,
      sections: [{
        section_key: 'chief_complaint',
        text: `loadtest body ${Date.now()}`,
        transcript_segment_ids: [],
        icd10: [],
        field_specific_metadata: {},
      }],
      icd10_codes: [],
    },
  });
  const res = http.put(`${baseUrl}/v1/reports/${id}/draft`, body, { headers });
  autosaveLatency.add(res.timings.duration);
  if (res.status === 409) {
    autosaveConflict.add(1);
    autosaveOk.add(false);
  } else {
    autosaveOk.add(res.status === 200);
  }
  sleep(5);
}

export function searchScenario() {
  const qs = rand([
    'q=задишк', 'q=chest', 'q=', 'status=draft', 'status=signed',
    'icd10=I21', 'icd10=E11.9&status=finalized',
  ]);
  const res = http.get(`${baseUrl}/v1/reports/search?${qs}&limit=25`, { headers });
  const hasQ = /q=[^&]+/.test(qs) && !/q=$/.test(qs);
  searchLatency.add(res.timings.duration, { has_q: String(hasQ) });
  check(res, { 'search 200': r => r.status === 200 });
}

export function diffScenario() {
  const id = rand(reportIds);
  const res = http.get(`${baseUrl}/v1/reports/${id}/diff?from=1&to=2`, { headers });
  diffLatency.add(res.timings.duration);
  check(res, { 'diff 200 or 404': r => r.status === 200 || r.status === 404 });
}
