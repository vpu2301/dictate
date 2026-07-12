// Sprint-10 load proof for autocomplete-service (step 08).
//
// Driven by scripts/loadtest/run-sprint-10.sh (warmup → scenarios →
// Prometheus scrape → results doc). Scenario selection via -e SCENARIO=
//   main   — sustained 100 RPS for 10m (warm cache) + burst 500 RPS for 1m
//   storm  — cold-storm: 100 RPS for 1m against a freshly flushed cache
//            (the wrapper deletes autocomplete:trie:* keys first)
//   chaos  — sustained 100 RPS for 3m while the wrapper stops Redis for 60s
//
// Budget semantics: 80 ms is the cache-hit TARGET (reported); 150 ms is the
// machine-enforced hard-fail line (local-stack noise headroom). 5xx are a
// hard zero in every scenario — degradation is latency-only, never errors.
//
// BEARER accepts a comma-separated token list; requests rotate through it so
// per-(tenant,language,user) cache keys are genuinely exercised.

import http from 'k6/http';
import { check } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const baseUrl = __ENV.BASE_URL || 'http://localhost:8007';
const authUrl = __ENV.AUTH_URL || 'http://localhost:8000';
const which = __ENV.SCENARIO || 'main';

// Per-VU auth with re-login on 401: the Keycloak dev access token lives
// ~11 min — a static bearer 401s the tail of the 10-min sustained stage
// and the whole burst (observed: 31.6k 401s). Real clients refresh; so
// does the load test. Rotating seeded users exercises distinct
// per-(tenant,language,user) trie keys.
const USERS = [
  'clinician@tenant-a.example',
  'admin@tenant-a.example',
  'admin@tenant-b.example',
];

const suggestLatency = new Trend('mdx_suggest_latency_ms', true);
const ok = new Rate('mdx_suggest_ok_rate');
const serverError = new Rate('mdx_suggest_5xx');

// Realistic mix: 1–6-char slices of the seeded corpus, uk-heavy (~85/15),
// ~5% snippet triggers.
const UK_STEMS = [
  'задишка при фізичному навантаженні', 'біль за грудиною стискаючого характеру',
  'ритм синусовий правильний', 'тони серця ясні ритмічні',
  'інфаркт міокарда в анамнезі', 'гіпертонічна хвороба',
  'температура тіла нормальна', 'шкіра звичайного кольору',
  'свідомість ясна', 'загальний стан задовільний',
  'цукровий діабет 2 типу', 'глікемія натще',
  'без вогнищевої патології', 'легеневі поля прозорі',
];
const EN_STEMS = [
  'shortness of breath on exertion', 'chest pain radiating to left arm',
  'regular sinus rhythm', 'history of myocardial infarction',
  'no acute distress', 'alert and oriented x3',
];
const SNIPPETS = ['/cv', '/vitals', '/ecg', '/plan'];

function pick(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

function makeRequest() {
  const r = Math.random();
  let prefix;
  let language;
  if (r < 0.05) {
    prefix = pick(SNIPPETS);
    language = Math.random() < 0.8 ? 'uk' : 'en';
  } else if (r < 0.85) {
    const stem = pick(UK_STEMS);
    prefix = stem.slice(0, 1 + Math.floor(Math.random() * 6));
    language = 'uk';
  } else {
    const stem = pick(EN_STEMS);
    prefix = stem.slice(0, 1 + Math.floor(Math.random() * 6));
    language = 'en';
  }
  return JSON.stringify({ prefix: prefix, language: language, limit: 3 });
}

const SCENARIOS = {
  main: {
    sustained: {
      executor: 'constant-arrival-rate', rate: 100, timeUnit: '1s',
      duration: '10m', preAllocatedVUs: 60, maxVUs: 200, exec: 'suggestScenario',
    },
    burst: {
      executor: 'constant-arrival-rate', rate: 500, timeUnit: '1s',
      duration: '1m', preAllocatedVUs: 200, maxVUs: 600, exec: 'suggestScenario',
      startTime: '10m15s',
    },
  },
  storm: {
    cold_storm: {
      executor: 'constant-arrival-rate', rate: 100, timeUnit: '1s',
      duration: '1m', preAllocatedVUs: 120, maxVUs: 300, exec: 'suggestScenario',
    },
  },
  chaos: {
    chaos_sustained: {
      executor: 'constant-arrival-rate', rate: 100, timeUnit: '1s',
      duration: '3m', preAllocatedVUs: 120, maxVUs: 300, exec: 'suggestScenario',
    },
  },
};

export const options = {
  scenarios: SCENARIOS[which],
  thresholds: {
    mdx_suggest_latency_ms:
      which === 'main' ? ['p(95)<150'] : ['p(95)<1000'], // storm/chaos: latency may rise; errors may not
    mdx_suggest_ok_rate: ['rate>0.99'],
    mdx_suggest_5xx: ['rate==0'], // degradation is latency-only, NEVER errors
  },
};

let vuToken = null;
let vuUser = null;

function login() {
  vuUser = vuUser || USERS[(__VU - 1) % USERS.length];
  const r = http.post(
    `${authUrl}/auth/login`,
    JSON.stringify({ email: vuUser, password: 'dev-password' }),
    { headers: { 'Content-Type': 'application/json' }, timeout: '10s' },
  );
  vuToken = r.status === 200 ? r.json('access_token') : null;
}

function post(body) {
  return http.post(`${baseUrl}/autocomplete/suggest`, body, {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${vuToken || ''}`,
    },
    timeout: '10s',
  });
}

export function suggestScenario() {
  if (!vuToken) login();
  const body = makeRequest();
  let res = post(body);
  if (res.status === 401) {
    // token expired mid-run — refresh exactly like a real client would
    login();
    res = post(body);
  }
  suggestLatency.add(res.timings.duration);
  ok.add(res.status === 200);
  serverError.add(res.status >= 500);
  check(res, { '200 OK': (r) => r.status === 200 });
}
