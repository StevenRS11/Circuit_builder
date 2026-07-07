# Field Report — {project_name} rev {rev} — {YYYY-MM-DD}

<!--
This is the ten-minute ritual that converts bench time into permanent
capability. Fill it after any bench session (first bringup OR later field
use). Then run the promotion pass (references/promotion.md) and update
validated_boards.yaml — check_ledger.py enforces that the claims hold.
-->

Bench setup: {supply, load, instruments}
Checklist: {path to the completed bringup checklist, or "field use, no checklist"}
Board serial / build: {which physical unit(s)}

## Checklist results

| Section | Result | Notes |
|---------|--------|-------|
| 0 · Before power | PASS / FAIL | |
| 1 · First power | | |
| 2 · Digital | | |
| 3 · Functional | | |

## Anomalies & failures

<!-- One block per anomaly. "Works but X was odd" counts — oddities are
     tomorrow's failures. -->

### A1 — {short title}
- **Symptom:** {what was observed, with numbers}
- **Root cause:** {confirmed / suspected — say which, and the evidence}
- **Fix applied:** {rework done, rev change needed, or "none yet"}
- **Lesson class:** pinout / topology / sourcing / layout / process
- **Where it must be encoded:** {per the routing table in
  references/promotion.md — gate, check, test, recipe, reference doc}

## Successes worth promoting

- [ ] Board stable → status `validated` in ledger
- [ ] Subcircuit(s) proven → block-extraction candidates: {refs + function}
- [ ] Novel IC pinouts confirmed in silicon → promote fact cards to
      `pinout_db.json` (`pinout_verified: true` is now *bench*-verified)
- [ ] Board → Tier-3 eval anchor candidate

## Deposits made (the part that must not be skipped)

| # | Deposit | Where | Done |
|---|---------|-------|------|
| 1 | {e.g. new preflight gate for defect A1} | {file/test} | [ ] |
| 2 | Ledger entry updated | validated_boards.yaml | [ ] |
| 3 | `check_ledger.py` passes | | [ ] |
