# Outstanding human / business actions

## 🔴 REDEPLOY REQUIRED — streaming transcripts were empty in every session

Found 2026-07-26 by driving conversation mode end-to-end against the
deployed compose stack (the first time the WS path ran with real audio
from outside the test suite). **Fixed in source; the running images are
stale.** Rebuild before any further live testing:

```
docker compose build dictation-service core-service && docker compose up -d
```

Two latent defects, both invisible to the existing suites:

1. **`_emit_tick` could not serialize a single partial.** The handler
   passed `asr_models.WordTiming` objects into the wire models'
   `words: list[TokenTiming]` field — field-identical but a different
   class, and pydantic v2 does not coerce one `BaseModel` into another.
   The `ValidationError` escaped into `_window_loop`, a bare
   `create_task` with no error path, so the window loop **died on the
   first partial of every session in both protocol versions**. The
   session kept accepting audio, kept looking healthy to the clinician,
   stored its audio, and finalized an **empty transcript** — silently.
   Fixed with `_wire_words()`; the window loop is now guarded per tick
   and fails the session loudly after 3 consecutive failures rather
   than transcribing nothing.
2. **`GET /dictate/sessions/{id}` 500'd on every read.**
   `transcript_jsonb` comes off asyncpg as a JSON *string* (no codec is
   registered — the same reason finalize writes it with `json.dumps`),
   and the response model rejected it. The conversation review swallows
   that error by design, which is why it never surfaced.

Also added: `kind='scribe'` rows on `GET /patients/{id}/timeline`, so a
finished conversation is reachable from the patient card at all (it
previously left only an opaque `kind='recording'` audio row).

**Standing gap this exposed** — nothing exercises the WS wire with real
audio in CI. The unit suites drive the windower and the codec
separately, and the chaos/load suites use synthetic Opus that never
reaches a commit decision, so a defect *between* them ships green.
Owner: tech lead. Consider a nightly job running
`scripts/eval/run_conversation_e2e.py` **over the socket** rather than
in-process.

## 🔴 PRE-EXISTING BREAKAGE found during S14 — S13 template regression

- [ ] **`required: true → false` on the diagnosis/assessment sections of
      all 20 shipped templates** (owner: clinical content lead + tech
      lead). Commit `991fc20` (S13) flipped the flag in
      `infra/seeds/templates/*.json` while converting those sections to
      `field_type: structured_diagnosis`. By the schema's own doctrine
      (`classify_edit`, ADR-0016) a flipped `required` is a
      **STRUCTURAL** template change, and clinically it means the
      diagnosis section is now optional in every template.
      `libs/template_models/tests/unit/test_schema.py::test_all_seed_templates_validate_and_dump_byte_identical`
      has been failing on the branch ever since — the gate worked; the
      change shipped anyway. **`make test` / `make ci` are red for this
      reason alone**, independent of S14.
      Decide: (a) intentional → re-freeze the fixtures with a recorded
      ADR-0016 amendment, or (b) accidental → restore `required: true`
      in the seeds. S14 deliberately did NOT regenerate the frozen
      dumps, because doing so would erase the only evidence.

## Conversation mode & diarization (S14)

- [ ] **A10G rig DER gate** (owner: SRE/DevOps + tech lead). The
      diarization numbers in ADR-0034 are CPU (Apple M5) plumbing
      numbers, per the ADR-0019 WER precedent. Before conversation mode
      ships to staging, run `make der-eval` on the A10G rig with both
      models resident and record: DER, per-window latency alongside 4
      Whisper sessions, VRAM headroom. The same missing rig already
      blocks the sprint-07 WER gate (docs/sprint-07/SPRINT-TODO.md).
- [ ] **🔴 A10G rig CAPACITY gate — blocks conversation mode reaching
      patients** (owner: SRE/DevOps + tech lead). `make capacity-probe`
      on the rig with both models resident. ADR-0035 records what the
      CPU laptop could and could not establish:
      **could** — diarization costs 33–47 ms/window and +166 MB RSS
      (+46 % over Whisper), neither growing with concurrency;
      **could not** — whether dictation partial p95 ≤ 1100 ms survives
      co-tenancy. That is NOT EVALUABLE on CPU/`tiny` and the probe
      refuses to print a verdict for it. A paired trial also produced a
      **6× unreproducible spread** in the Whisper path under co-tenancy
      (p50 2870 ms then 499 ms for identical configs) that could not be
      separated from host contention — the rig must settle whether that
      is real interference (→ split the fleet) or laptop noise.
      Record: VRAM with Whisper alone vs both models; combined
      per-window latency at 1 and 2 conversation sessions; dictation
      partial p95 with 2 conversation sessions live.
- [ ] **Conversation session weight is deliberately conservative, not
      measured on the target device** (owner: tech lead).
      `MDX_CONVERSATION_SESSION_WEIGHT=2` (4 dictation OR 2 conversation
      per worker). CPU evidence says a conversation session costs ~1.5×
      a dictation session on memory and ~1.1× on compute, i.e. weight 2
      under-books the worker — intentionally, because refused sessions
      are recoverable and degraded live transcription is not (ADR-0035).
      Re-tune on the rig; **lower it only on rig evidence.**
- [ ] **Three sprint-04 metrics are still declared-but-never-emitted**
      (owner: tech lead). The sprint-14 deployment pass found that
      `metrics.py` declared instruments no code ever wrote, so the
      sprint-04 dashboard and its latency alerts had been querying empty
      series since sprint 04. Now emitted: `active_sessions`,
      `model_loaded`, `partial_latency_ms`, `final_latency_ms`,
      `window_inference_ms`, `rtf`, `reconnects_total`,
      `ws_upgrade_rejections_total`. **Still dormant:**
      `opus_decode_us`, `bandwidth_bps`, `audio_decode_errors_total` —
      all three live on the per-frame audio path (decoder/buffer), none
      is referenced by an alert, so they were left out of this sprint's
      scope rather than half-wired. Either emit them or delete the
      declarations; a declared-but-empty metric reads as "healthy".
- [ ] **Whisper weights are not startup-verified** (owner: tech lead).
      Sprint 14 added a fail-closed startup checksum assertion for the
      ECAPA weights (`diarization/integrity.py`); `MD_ASR_MODEL_SHA256`
      is still logged as provenance only. Extend the same assertion to
      the ASR weights in asr-worker/dictation-service.
- [ ] **pyannote gated weights — decision recorded, revisit only with
      process** (owner: tech lead + security lead). pyannote 3.x was
      desk-rejected (ADR-0034): HF-gated weights with no gated-model
      process in the platform, and network-resolving pipeline config vs
      the offline bake. If SOTA DER is ever needed, first define the
      gated-weights acceptance/custody process, then re-open with an ADR.
- [ ] **Real two-speaker eval audio** (owner: clinical content lead +
      DPO). `eval/conversations/v1` is synthetic TTS with generator
      ground truth. Real consented consultation recordings (or acted
      scripts) with hand-labeled turns are needed before the DER bar is
      a clinical claim; the PII sweep + consent path for that corpus is
      DPO territory.

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
