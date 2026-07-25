# Outstanding human / business actions

## Structured anamnesis (S13)

- [ ] **May a tenant finalize with auto-promoted ICD-10 proposals?** —
      owner: **clinical lead** (+ DPO for the billing angle). Sprint 13
      shipped `require_confirmed_diagnosis_on_finalize` (default
      **true**) governing MESSAGING only: with proposals present and
      nothing confirmed, finalize is blocked either way
      (`diagnosis_not_confirmed` when true, `missing_icd10` when
      false). The sprint doc left room for reading `false` as
      "auto-promote the proposals at finalize"; that was **rejected**
      in implementation because it would put a machine-chosen
      diagnosis into a signed clinical record, contradicting the
      never-guess directive. If clinical policy decides some tenant
      may opt into auto-promotion, the change is contained to
      `finalize_validator._typed_problem` + this flag. Rationale:
      `docs/architecture/reports.md`.
- [ ] **Tenant-settings mechanism** — owner: **tech lead**. The repo
      has no per-tenant settings store (no `settings` JSONB on
      `tenants`, and the `require_patient_on_finalize` precedent the
      S13 plan cited does not exist). S13's confirmation flag is
      therefore platform-wide service config.
      `validate_finalize(require_confirmed_diagnosis=...)` already
      takes the value as an argument, so wiring per-tenant resolution
      is a one-line change once a settings store lands.

- [ ] **Acquire the full МКХ-10-АМ table** — owner: **clinical lead +
      ops**. Sprint-13 shipped the reference table (migration 0054),
      the idempotent loader (`scripts/load-icd10.py`), and search, but
      only a **239-code hand-checked fixture**
      (`infra/seeds/icd10/fixture.csv`) — not the full ~14 000-code
      classifier. Ukraine mandates МКХ-10-АМ (НК 025:2021, the
      Australian modification); a timeboxed search found no official
      МОЗ/НСЗУ download under clear redistribution terms — it moves
      through eHealth central-database dictionaries and commercial
      publications, and the AM base is licensed. Needed: (a) the
      authoritative file, (b) written confirmation we may load and
      serve it, (c) a re-check that the loader's `CODE_RE` and
      migration 0054's CHECK match the real file's dialect. Until
      then, codes outside the fixture cannot be proposed or picked —
      clinicians dictate those diagnoses as prose (nothing is
      mis-coded, only un-coded). Procedure: `docs/runbooks/icd10.md`.

- [ ] **`anamnesis_intake` template wording review** — owner:
      **clinical content lead** (+ linguist). Sprint-13 shipped the
      new system template
      (`infra/seeds/templates/anamnesis_intake.json`) with
      engineering-authored plausible wording: section names/prompts,
      the smoking-status option set (never/current/former) and the
      allergen option set (none_known, penicillin, nsaids,
      iodine_contrast, local_anesthetics, latex, pollen, food, other),
      plus uk/en voice aliases for each option. Review labels, the
      allergen list composition, and alias coverage (gendered verb
      forms, палити/курити synonyms) before pilot use. Alias edits are
      cosmetic (no new template row); removing/renaming an option
      `value` is structural — see ADR-0016 amendment.

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

- [ ] **DSAR subject-accessible audit-kind allowlist** — owner: **DPO**.
      `DSAR_AUDIT_KINDS` ships with a conservative lifecycle-only default
      (patient/consent/privacy kinds). Widening what a patient sees of
      the audit trail is a policy decision — config change only
      (docs/runbooks/erasure.md).
- [ ] **Raw audio in DSAR packages** — owner: **DPO**.
      `DSAR_INCLUDE_RAW_AUDIO=false` ships; the manifest/README say
      "available on request". Flipping it streams decrypted recordings
      into the package — config change only.
- [ ] **Runbook patient-explanation wording (uk) review** — owner:
      **clinical lead**. The basis→human-text table in
      docs/runbooks/erasure.md will be read to actual patients; review
      before pilot use.
- [ ] **Clinical-record retention period confirmation** — owner:
      **legal counsel**. The erasure engine retains signed reports for
      `REPORT_RETENTION_YEARS` (default 25, per the common МОЗ
      clinical-record retention reading). Confirm the exact period for
      the pilot clinic's record classes before the first production
      erasure; the config flip is the whole change
      (docs/architecture/erasure.md).
- [ ] **Consent text legal review** — owner: **legal counsel +
      clinical lead**. `infra/seeds/consents/*.md` (ai_scribe-v1,
      data_processing-v1) are engineering drafts; the КЕП signature
      binds their exact bytes (S11 step 03), so wording changes after
      review must ship as NEW versions (`-v2.md`), never edits. Review
      required before pilot use of digital consents.

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
