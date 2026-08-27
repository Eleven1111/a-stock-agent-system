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

### Strategy Shadow

The non-live evaluation lane that consumes the Strategy Evidence Dataset. A
shadow result cannot affect ranking, positions, the signal ledger, or orders.
Missing evidence is `unavailable`, never `no_signal`.

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
