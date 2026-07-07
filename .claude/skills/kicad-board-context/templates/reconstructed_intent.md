# Reconstructed Design Intent — {board_name}

The brownfield equivalent of a Stage-1 spec: what this board *appears* to be
for, derived from its extracted artifacts. Every claim is marked:

- **[INFERRED]** — Claude's reading of the artifacts; may be wrong.
- **[CONFIRMED]** — the user has verified it.
- **[OPEN]** — a question the artifacts can't answer.

This document is only useful after the user has corrected it. Present it,
ask what's wrong, upgrade the marks. Once confirmed, it is the reference
that reviews, debug hypotheses, and modification specs are checked against —
treat [CONFIRMED] statements the way the generator skill treats approved
spec requirements.

## Purpose

[INFERRED] {one paragraph: what the board does, for what larger system}

## Power tree

[INFERRED]
```
{source, e.g. USB-C 5V} ──{U?}──> {rail} ({voltage}, est. loads: {refs})
                          └─{U?}──> {rail} ...
```
- Rail budgets: [OPEN] {unknown from artifacts — ask user or derive worst-case}

## Functional blocks

| Block | Parts | Role | Mark |
|-------|-------|------|------|
| {e.g. Charger} | U2, L1, R8... | {what it does} | [INFERRED] |

## External interfaces

| Connector | Signals | Goes to | Mark |
|-----------|---------|---------|------|
| J1 | {nets} | {inferred counterpart} | [INFERRED] |

## Sensitive / notable nets

Net `class:` tags applied to `netlist.yaml` during enrichment, with reasoning:

| Net | class | Why | Mark |
|-----|-------|-----|------|
| {NET} | {analog_differential ...} | {e.g. bridge sense pair into U3} | [INFERRED] |

## Known history (from the user)

- {revision history, past failures, bench observations the user shares —
  these are [CONFIRMED] by definition}

## Open questions

1. [OPEN] {things the artifacts cannot answer; each blocks some class of
   reasoning — say which}
