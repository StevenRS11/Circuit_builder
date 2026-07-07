# Findings — {board_name} — {YYYY-MM-DD} — {topic}

One dated document per session/topic, accumulating in `findings/`. Findings
survive the session: a later conversation reads these before re-deriving
anything. Ruled-out hypotheses are as valuable as confirmed defects.

**Session context:** {mode: review / debug / explain / reselect / modify},
prompted by: {user's ask, one line}. Context pack at sch sha256 `{short}`,
pcb `{short}`.

## Findings

### F1 — {short title}
- **Severity:** blocker / major / minor / info
- **Status:** open / fix-agreed / fixed-and-reverified / accepted-as-is
- **Location:** {refs, nets, or board area}
- **Evidence:** {which script output / datasheet page / measurement — cite,
  don't assert. e.g. "reconcile.py field_not_propagated on U1; datasheet
  CH224K p.4 pin table via datasheets/CH224K.pdf"}
- **Issue:** {what is wrong and why it matters electrically}
- **Suggested fix:** {specific: change R5 to 12k / rerun F8 / move C3 within 3mm of U2.VDD}

## Ruled out

| Hypothesis | Ruled out by | Date |
|------------|--------------|------|
| {e.g. "I2C pull-ups missing"} | {netlist.yaml shows R3/R4 on SDA/SCL to +3V3} | {date} |

## Measurements requested / received

| # | Measurement | Why (discriminates between) | Result |
|---|-------------|------------------------------|--------|
| 1 | {e.g. voltage at U2.FB under load} | {H1 vs H2} | {value or pending} |

## Follow-ups

- [ ] {action} — owner: {user / Claude next session}
