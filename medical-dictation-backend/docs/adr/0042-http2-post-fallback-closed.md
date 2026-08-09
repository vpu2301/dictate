# ADR-0042: HTTP/2 POST streaming fallback — closed with data, not built

Date: 2026-08-08
Status: Accepted (IOU closed — "not needed, monitored")
Sprint: 16 (deployment)

## Context

ADR-0012 benched an HTTP/2 chunked-POST transport as the fallback for
clients behind WS-stripping proxies, with "sprint 16 evaluates" on the
bench note. The evaluation criterion set by the sprint-16 spec: **decide
with data** — if pilot telemetry shows proxy-stripped upgrades in the
field, build it; if the metric is ~zero, close the IOU.

## The data

`mdx_dictation_ws_upgrade_rejections_total` is emitted at the single
choke point every rejection passes through
(`ws/upgrade.py` — instrument wired in the sprint-14 deployment pass
after being declared-but-silent since sprint 04). Queried on the pilot
Prometheus (2026-08-08, full retention window):

```
query: mdx_dictation_ws_upgrade_rejections_total   → result: []  (series absent)
```

An OTel counter exports only after its first increment — an absent
series means **zero rejections ever recorded**. The pipeline itself is
proven live by sibling series from the same meter
(`mdx_dictation_active_sessions_ratio`,
`mdx_dictation_conversation_sessions_total`, capacity gauges — all
present and used by the KEDA scaler), so the absence is evidence of
zero events, not of a dead pipeline.

## Decision

Close the IOU as **"not needed, monitored"**. No fallback transport is
built. The counter stays wired; the trigger to reopen is explicit:

- any sustained non-zero rate of upgrade rejections with reasons that
  indicate proxy interference (as opposed to auth/capacity codes), or
- a clinic deployment landing behind a known WS-hostile corporate proxy.

The `DictationUpgradeRejectionRate` alert (sprint-14) is the standing
tripwire; a fired alert re-opens this ADR with field data attached.

## Consequences

- Zero speculative transport code to maintain; the WS path stays the
  single streaming wire (medical-dictation.v1/v2).
- If the trigger fires, ADR-0012's bench design (seq-framed chunked
  POST, same session protocol) is the starting point.
