# Web source allowlist — trust tiers (v1)

The reviewed artifact behind two machine-readable lists. **This table is the
source of truth**; the other two follow it.

| | File | Changed by | Effect |
|---|---|---|---|
| Product | `web_domains` seed in migration `0070_evidence_answers.sql` | migration / `POST /web/domains` | which sources may be *cited* |
| Network | `infra/compose/egress-proxy/allowlist.txt` | operator, via deploy | which hosts may be *reached* |

A domain enabled in the product list but missing from the network list simply
fails to fetch. That asymmetry is the point (ADR-0005): a runtime control can
never widen the network boundary. `tests/security/test_egress_policy.py`
asserts the seeded product domains are all network-reachable.

## Trust tiers

Tier is about **source trust**, not evidence strength — a WHO fact sheet and a
WHO systematic review share a tier and differ in `evidence_tier`. Tiers use
the frozen `WebTrustTier` contract vocabulary (ADR-0002, extended in S04 with
`international_organization`).

| Tier | Means | Presentation |
|---|---|---|
| `international_organization` | Intergovernmental body (WHO, EU agencies) | Highest web tier; ranked above national guidance only when more recent |
| `government` | A state's health authority or agency | National guidance; `moz.gov.ua`/`dec.gov.ua` outrank foreign agencies for UA jurisdiction |
| `guideline_registry` | Curated multi-society guideline collections | Trusted index, not a primary author |
| `professional_society` | Specialty society publishing its own guidance | Authoritative within its specialty |
| `journal` | Peer-reviewed publisher | Mostly paywalled → metadata-only |
| `other` | Anything else a tenant adds | Requires review; never a default |

## Shipped set (seeded under the reserved GLOBAL tenant)

`metadata-only` means: cite the title, URL and access date; never extract or
quote body text (§7 — paywalled/licensed sources).

| Domain | Tier | Metadata only | Notes |
|---|---|---|---|
| `who.int` | international_organization | no | WHO guidelines, fact sheets |
| `iris.who.int` | international_organization | no | WHO institutional repository (full guideline text) |
| `ecdc.europa.eu` | international_organization | no | European CDC |
| `ema.europa.eu` | international_organization | no | EMA — SmPCs, safety communications |
| `cdc.gov` | government | no | US CDC |
| `fda.gov` | government | no | US FDA — labels, safety communications |
| `nice.org.uk` | government | no | NICE (UK) guidance |
| `moz.gov.ua` | government | no | МОЗ України — накази, клінічні настанови |
| `dec.gov.ua` | government | no | ДЕЦ МОЗ — держреєстр ЛЗ, настанови |
| `pubmed.ncbi.nlm.nih.gov` | government | **yes** | Abstract pages: metadata + abstract only |
| `ncbi.nlm.nih.gov` | government | no | PMC open access, Bookshelf |
| `g-i-n.net` | guideline_registry | no | Guidelines International Network |
| `magicevidence.org` | guideline_registry | no | MAGICapp published guidelines |
| `escardio.org` | professional_society | no | European Society of Cardiology |
| `acc.org` | professional_society | no | American College of Cardiology |
| `idsociety.org` | professional_society | no | IDSA |
| `ersnet.org` | professional_society | no | European Respiratory Society |
| `easl.eu` | professional_society | no | EASL (hepatology) |
| `kdigo.org` | professional_society | no | KDIGO (nephrology) |
| `ginasthma.org` | professional_society | no | GINA (asthma) |
| `goldcopd.org` | professional_society | no | GOLD (COPD) |
| `diabetes.org` | professional_society | no | ADA Standards of Care |
| `cochranelibrary.com` | journal | **yes** | Systematic reviews — abstract/metadata only |
| `bmj.com` | journal | **yes** | Metadata only |
| `thelancet.com` | journal | **yes** | Metadata only |
| `nejm.org` | journal | **yes** | Metadata only |

## Per-tenant changes

Resolution is **tenant-first**: a tenant row for a domain shadows the shipped
GLOBAL row.

- **Add** a domain a tenant trusts: `POST /web/domains` (`evidence.domains.manage`).
  New rows land as `status='pending'` — allowlisting is a review step, not a
  self-service toggle. It also does nothing until the network allowlist
  permits the host.
- **Disable a shipped default**: insert a tenant row for the same domain with
  `status='disabled'`. The shipped table is never mutated, so it stays a
  reviewed artifact rather than something 40 tenants have quietly diverged
  from.
- Every change writes `evidence.web_domain_added` / `…_disabled` at security
  severity into the shared audit chain.

## Adding a domain to the shipped set

1. Add a row to the table above with its tier and the clinical reviewer.
2. Add the host to `infra/compose/egress-proxy/allowlist.txt`.
3. Add the `INSERT` to the migration seed (or a follow-up migration).
4. Run `make security-web` — the cross-check test must stay green.

## Permanently excluded

Model-vendor APIs (`api.openai.com`, `api.anthropic.com`,
`generativelanguage.googleapis.com`, …). All inference is self-hosted
(rule LM1), and the network allowlist is where that stops being a claim.
`test_no_model_vendor_is_reachable` enforces it.

## Review status

| Item | Reviewer | Status |
|---|---|---|
| Tier definitions | clinical advisor | ⏳ **required** |
| Shipped 26-domain set | clinical advisor | ⏳ **required** |
| Metadata-only designations (licensing) | tech lead + clinical advisor | ⏳ |
| Network/product split | security lead | ⏳ |
