---
id: event-example-guidance-revision
type: event_type
status: example
title: Example guidance-revision event
aliases:
  - synthetic revision event
related:
  - relation: contradicts
    target: thesis-example-relative-strength
    sign: negative
    horizon: 1-trading-day
    observations: 0
    uncertainty: unknown
    as_of: null
    provenance: source-schema-example
    causality: non_causal
  - relation: invalidates
    target: thesis-example-relative-strength
    sign: negative
    horizon: 20-trading-days
    observations: 0
    uncertainty: unknown
    as_of: null
    provenance: source-schema-example
    causality: hypothesis
---

# Example guidance-revision event

A synthetic event type showing how contradictory evidence and a hypothetical
invalidation rule can be encoded. Neither edge describes an observed event.
