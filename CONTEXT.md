# Domain Context

## Strategy research language

### Strategy Evidence Dataset

The immutable, point-in-time daily dataset that gives all six research
strategies their inputs. It owns source provenance, availability, cohort
selection, field derivation and completeness reporting. Strategy modules do
not merge provider artifacts themselves.

### Evidence Cohort

The bounded union of the day's official limit-up events, the auction
shortlist, and leaders already being followed across days. It is the only
universe eligible for the once-after-close minute request. It is not a
full-market minute database and must never be silently truncated.

### Canonical Forward Evidence

Evidence frozen from sources available on or after the decision date, with an
explicit observation time and source. It may enter forward shadow evaluation
after its point-in-time cutoff. It is the only evidence eligible for a future
out-of-sample gate.

### Exploratory Reconstruction

Approximate historical evidence reconstructed from lower-resolution data. It
can support bias audits and exploratory research, but cannot enter canonical
forward samples or the research gate.

### Evidence Qualification

The derived, strategy-scoped classification of observed inputs as
`canonical_forward`, `exploratory_reconstruction`, or `unavailable`. It is
computed from field provenance (`observed_at` and source identity), never from
a producer-supplied boolean. A Strategy Evidence Dataset may therefore be
`mixed`; only strategies classified `canonical_forward` may create settleable
forward samples.

For S2, `turnover_baseline_median_pct` specifically means cumulative turnover
at the same clock minute as the reseal across prior sessions. Full-day daily
`turn` is a different measure and is rejected as legacy-invalid evidence.

### Strategy Shadow

The non-live evaluation lane that consumes the Strategy Evidence Dataset. A
shadow result cannot affect ranking, positions, the signal ledger, or orders.
Missing evidence is `unavailable`, never `no_signal`.

### Settled Forward Sample

An immutable, research-only outcome joined to one eligible Strategy Shadow
prediction after that prediction was frozen. Entry is the next trading
session's open reference; T+1 and T+3 use market sessions, with CSI300 measured
over exactly the same sessions. The sample owns its prediction, rules, policy,
bar-snapshot and evidence hashes plus explicit estimated costs and slippage.

Only `final`, primary-horizon samples whose Evidence Qualification is
`canonical_forward` and whose rules and settlement policy hashes are approved
may enter the research gate. Pending and terminal-unresolved predictions stay
in the coverage denominator. Settled Forward Samples never enter the signal
ledger and cannot affect ranking, positions or orders.

### Tradeable Leader Binding

The point-in-time security identity that turns S6's market-level ice-point
condition into a settleable prediction. It must name a real six-digit A-share
candidate whose leader confirmation and qualifying shadow score were already
present in the Strategy Evidence Dataset. `MARKET`, an inferred proxy, or a
bare code supplied later by Strategy Shadow is never a Tradeable Leader
Binding. Without the binding S6 is `unavailable` and creates no Settled
Forward Sample.

### Daily-Bar Source Health

The per-run account of how the historical daily-bar cache was populated. It
distinguishes BaoStock primary health from fallback completion, reports the
actual row and stock contribution of each provider, and discloses source
concentration. A successful fallback run remains operationally successful but
its source health is `degraded`; it must never imply that BaoStock was healthy.
If no second provider was sampled, cross-source consistency is explicitly
`unavailable` rather than inferred from a single source.

### Four-Dimension PIT Replay

The research-only point-in-time replay of Four-Dimension inputs. Its first
version exposes only the technical dimension reconstructed from local QFQ
daily bars: a decision on session D may read bars dated no later than D, enters
at the D+1 open reference, and settles T+1/T+3 closes against CSI300 over the
same sessions with estimated costs and bilateral slippage.

The technical Adapter uses the canonical scorer's 60-session input window.
Chan structure is disabled in historical replay because today's research-gate
registry is not point-in-time evidence; with every Chan rule ineligible its
numeric delta and score lock are already zero, so skipping its display-only
analysis preserves the numeric technical score and its scoring indicators while
avoiding repeated work.
The Adapter also omits observable-proxy and emotion-cycle payload construction:
neither participates in the technical score, and neither is published by this
replay artifact.
The default exploratory policy evaluates five held-out sessions per
walk-forward fold across the full cached cross-section; this sparse schedule
keeps the initial 268-session replay bounded and must be disclosed rather than
presented as continuous daily coverage.
Variants are defined by interpretable daily rank capacity (`top5`, `top10`,
`top20`, `top50`). Duplicate policy signatures are invalid. If distinct
signatures still realise the same candidate/outcome set, the artifact marks
the comparison `redundant` and makes it ineligible for parameter comparison;
identical metrics must never be presented as independent evidence.

Sentiment, catalyst and deep-research dimensions remain `unavailable` until
their own historical point-in-time snapshots exist; current caches must never
be used to fill historical decisions. The Module emits immutable
`exploratory_reconstruction` artifacts under purged walk-forward splits. They
cannot enter the research gate, generate an Order, or automatically change
live Four-Dimension weights.

## Relationships

```text
frozen daily artifacts + official close event pool + bounded minute snapshot
    -> Strategy Evidence Dataset
        -> six strategy adapters
            -> Strategy Shadow
                -> settled forward samples
                    -> research gate (only after rules are locked)
```

Exploratory Reconstruction is deliberately outside that promotion path.

local QFQ daily bars -> Four-Dimension PIT Replay -> exploratory threshold diagnostics

That replay path remains deliberately outside the canonical promotion path.

## Example dialogue

- "Why is S2 unavailable today?" — "The Strategy Evidence Dataset has no
  official reseal event for that code; a 5-minute reconstruction is excluded."
- "Are we collecting the whole market every minute?" — "No. The Evidence
  Cohort is fetched once after close and the artifact records its exact size."
- "Can this signal affect tomorrow's ranking?" — "No. It is still in Strategy
  Shadow and has not passed the research gate."
