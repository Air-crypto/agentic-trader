# Trading knowledge graph

Markdown in this directory is the source of truth. `graph.json` is a generated,
reviewable index for the local viewer; do not hand-edit it.

The checked-in nodes are schema examples, not market claims, trading signals,
or investment advice. Every example edge has zero observations and no `as_of`
date so it cannot be mistaken for validated evidence.

## Relationship-learning boundary

The graph is the reviewable hypothesis layer, not the statistical ledger.
Immutable prediction batches and candidate outcomes live in Postgres under the
`learning_*` tables. The evening task may propose graph changes under
`artifacts/learning/kg-proposals.json`; a proposal does not change this source
of truth and cannot authorize a trade.

When a relationship is reviewed into Markdown, `observations` counts only
point-in-time observations with matching horizon and provenance. Record both
supporting and contradicting evidence. A larger count may reduce uncertainty,
but it never changes `causality: hypothesis` to proven causality. Regime,
sector, source, and model nodes should be used to preserve the conditions under
which a relationship was observed rather than collapsing it into a universal
rule.

## Node schema

Each node is a Markdown file with strict YAML-style frontmatter:

```yaml
---
id: example-security
type: security
status: example
title: Example security
aliases:
  - EXAMPLE
related:
  - relation: affected_by
    target: example-factor
    sign: positive
    horizon: 20-trading-days
    observations: 0
    uncertainty: unknown
    as_of: null
    provenance: source-schema-example
    causality: hypothesis
---
```

Allowed node types are `security`, `issuer`, `sector`, `factor`, `macro`,
`event_type`, `source`, `thesis`, and `model`.

Allowed directed relations are `supports`, `contradicts`, `affected_by`,
`benefits_from`, `hurt_by`, `co_moves_with`, and `invalidates`.

Every edge requires:

- `sign`: `positive`, `negative`, `neutral`, or `mixed`;
- `horizon`: a non-empty, human-readable time horizon;
- `observations`: a non-negative integer;
- `uncertainty`: `low`, `medium`, `high`, or `unknown`;
- `as_of`: an ISO `YYYY-MM-DD` date or `null`;
- `provenance`: the ID of a node whose type is `source`; and
- `causality`: `hypothesis` or `non_causal`.

The builder rejects causal relations (`affected_by`, `benefits_from`,
`hurt_by`, and `invalidates`) unless they are explicitly labelled
`causality: hypothesis`. It also requires `co_moves_with` to be non-causal.
These labels describe claim type, not confidence.

## Build and view

```bash
python scripts/build_graph.py
python scripts/build_graph.py --check
python scripts/serve_graph.py
```

Then open `http://127.0.0.1:8765/viewer/`. The viewer starts with every node
visible. Search, node-type filters, relation filters, and the sidebar narrow the
view without changing the Markdown source.
