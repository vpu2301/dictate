# Outstanding human / business actions

## Patient identity & privacy (S11)

- [ ] **Raw-ІПН retention decision** — owner: **DPO**. The platform
      stores the patient ІПН as an HMAC lookup token only;
      envelope-encrypted raw retention exists behind
      `PATIENT_IPN_RAW_ENABLED` (default **false**). Flip only with a
      documented lawful basis (Law 2297-VI data-minimization); the flag
      flip is the whole change (columns + crypto path already shipped,
      ADR-0027 decision B).
- [ ] **ІПН-hmac-at-erasure confirmation** — owner: **DPO**. ADR-0027
      records that erasure NULLs `ipn_hmac` (total identity
      destruction; no "previously erased" tombstone match on
      re-registration). Confirm, or direct the alternative (keep the
      hmac on the erased row for duplicate warnings — legal under the
      partial unique index, but retains a derived identifier of an
      erased person). Step-07 erasure engine consumes this decision.

## Autocomplete (S10 carry-over)

- [ ] **Full clinical corpus authoring (~10k UK / ~3k EN phrases, ~60
      snippets)** — owner: **clinical content lead**. Engineering ships
      only the 30-phrase starter set (migration 0026); unreviewed
      clinical content is a patient-safety risk and must not be
      authored by engineering. Workflow: author CSV/JSON per
      `infra/seeds/autocomplete/README.md` → run
      `scripts/validate-autocomplete-corpus.py` (PII + shape gate) →
      engineering renders `--emit-sql` into a migration PR → clinical
      sign-off on the PR.

## Signing (S09 revision)

- [ ] **Дія.Підпис test credentials** — request the free test
      environment (consultation → tech docs → test token via
      start@diia.gov.ua, accession agreement). Until they arrive the
      Дія flow is built and tested against the documented contract +
      recorded fixtures; the live test-environment round-trip is the
      remaining integration gap. Production contract/tariff is a
      further business step (no code change).
- [ ] **Production trust store** — `infra/trust-store/` ships the test
      CA only. Load the КНЕДП root/intermediate bundles from the CCA
      TSL (czo.gov.ua) via `scripts/update-trust-store.sh`, security
      review, PR merge (never auto-applied).
- [ ] **UAPKI TSA endpoint** — set `UAPKI_TSP_URL` to the chosen КНЕДП
      TSA in staging/prod so file_key envelopes upgrade CAdES-BES →
      CAdES-T (qualified timestamp). Offline dev signs CAdES-BES.
- [ ] **Legal counsel review** — server-side file-key custody consent
      text in the sign UI (ADR-0026 legal note; Law 2155-VIII sole
      control requirement).

## dev_password scaffold — REMOVE BEFORE LAUNCH

The development-only `dev_password` signing provider must be deleted
before production launch. Removal is deliberately small:

1. Delete `libs/kep/src/medical_kep/dev_password_provider.py` and its
   export lines in `libs/kep/src/medical_kep/__init__.py`.
2. Delete `services/signing-service/src/signing_service/keycloak_password.py`
   and the `enable_dev_password_provider` block in
   `services/signing-service/src/signing_service/config.py` +
   `providers.py` wiring.
3. Drop `"dev_password"` from the `provider` literals in
   `services/signing-service/src/signing_service/routers/inline.py` and
   `services/report-service/src/report_service/routers/reports_sign.py`;
   re-run `make openapi-dump`.
4. Remove `SIGNING_DEV_PASSWORD_ENABLED` from
   `docker-compose.override.yml`.
5. Keep migration 0034/0035 as-is (enum labels are immutable; the DB
   CHECK keeps any stray `dev_password` row pinned to the dev tier
   forever) and keep the CI gate as a tombstone.

Until removal, three independent guards keep it out of production:
provider constructor refusal, config-model rejection, and the
`check-no-dev-signing-in-prod-config` CI gate.

## Deployment (S09 deployment spec)

- [ ] **Public domain + TLS certificate** — the pilot has no public
      host yet. The exposure strategy is implemented and proven locally
      (reverse-proxy allowlist: `public-edge` nginx, TLS + edge rate
      limit, only `POST /signing/callbacks/diia` + `GET /verify/*`
      pass). To go live: point a public domain at the edge host, issue
      a Let's Encrypt cert, mount fullchain/privkey at
      `/etc/nginx/certs/edge.{crt,key}` (see
      `infra/edge/nginx.conf.template` header). Then repeat the curl
      VERIFY battery from a genuinely external network — Дія will not
      call self-signed endpoints.
- [ ] **CZO TL signer pin review** — `infra/trust-store/czo-cert.pem`
      was bootstrapped trust-on-first-use on 2026-07-04 (SHA-256
      A5:30:12:0C:62:EC:2F:32:FD:DB:09:F2:3B:B2:55:B0:E9:9C:09:63:01:BF:6D:D4:49:A6:6A:FA:5A:D4:CA:6F).
      Security lead must confirm this fingerprint against the CZO
      publication before production.
- [ ] **ca-bundle.pem PR review** — the applied 141-cert bundle
      (extracted from TL-UA.xml, xmlsec1-verified) is in the working
      tree; review as part of this sprint's PR. Note: `*.pem` is
      gitignored — decide whether trust-store bundles get a gitignore
      exception (recommended: yes, they are public certificates and
      the PR-gate depends on them being tracked) or move to a fetched
      volume in deploy tooling.
- [ ] **Дія API egress allowlist** — outbound 443 verified to
      czo.gov.ua / ca.diia.gov.ua / ca.informjust.ua; add the exact
      Дія partner API host once the tech docs arrive.
