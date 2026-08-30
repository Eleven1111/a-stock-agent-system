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

## Example dialogue

- "Why is S2 unavailable today?" — "The Strategy Evidence Dataset has no
  official reseal event for that code; a 5-minute reconstruction is excluded."
- "Are we collecting the whole market every minute?" — "No. The Evidence
  Cohort is fetched once after close and the artifact records its exact size."
- "Can this signal affect tomorrow's ranking?" — "No. It is still in Strategy
  Shadow and has not passed the research gate."
