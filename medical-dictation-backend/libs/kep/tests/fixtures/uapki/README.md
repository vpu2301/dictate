# UAPKI test fixtures

Vendored from the UAPKI project test data
(<https://github.com/specinfo-ua/UAPKI>, `library/test/data/`,
BSD-2-Clause — © СПЕЦІНФОСИСТЕМИ / specinfo-ua contributors).

- `test-diia.p12` — DSTU 4145 test key container, password
  `testpassword` (issued by the Дія test CA; expired — sign/verify in
  tests runs with `ignoreCertStatus` / offline mode).
- `certs/` — Дія test CA chain, TSP, and OCSP certificates.
- `crls/` — matching delta CRL.

Used ONLY by the `RUN_UAPKI_INTEGRATION=1`-gated tests that exercise
the real `libuapki.so` (Linux containers). These anchors are test CAs:
`TrustStore.is_test_anchor()` recognises them and any envelope chained
to them can never be reported as qualified.
