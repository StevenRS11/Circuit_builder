# Component Candidates — {PROJECT_NAME}

> For each role in the design, 2-3 candidate parts are presented with tradeoffs.
> **User picks the winner for each role before the BOM is finalized.**

## Selection Criteria

Boards are assembled by **PCBway turnkey** — PCBway sources every part from distributors (LCSC → DigiKey → Mouser) and populates the board. Part selection must pick parts PCBway can source and assemble cleanly (see the PCBway Part-Selection Rubric in SKILL.md Stage 2).

Every selected part **must pass all four gates** before moving to the BOM:
1. **Spec conformance** — satisfies every applicable Stage-1 requirement; **cite the datasheet for each [CRITICAL] one**. Failing any [CRITICAL] requirement disqualifies the part regardless of price/stock/library. **Every candidate the spec named must be evaluated** (or an explicit reason given for dropping it) — never substitute a requirement away.
2. **Works** — electrically correct for the application (specs match requirements)
3. **PCBway-sourceable** — in stock now at a PCBway distributor (LCSC PN captured) **and** an assembly-friendly package (standard SMT; no 01005, no bare die, THT/BGA only if intended)
4. **KiCad ready** — symbol and footprint exist in KiCad's built-in libraries (checked via `check_kicad_library.py --lookup`)

Additional ranking criteria:
4. **Preferred part** from `parts.preferred` / `connectors` / `preferred_regulators` (from preferences.yaml)
5. **Package** matches user preference order (SOT-23-5 > SOIC-8 > QFN, etc.)
6. **Distributor stock + second source** — more stock and an alternative source = lower order risk
7. **Datasheet quality** — clear application circuit, good documentation
8. **Price** — for equivalent parts, prefer cheaper

---

## Role: {functional role, e.g. "3.3V LDO Regulator"}

**Requirements:** {Vin range, Vout, Iout, dropout, features needed}

| # | Part              | Package   | Distributor PN | Stock     | Price (qty 1) | KiCad Lib?          | Key Specs                    | Tradeoffs                        |
|---|-------------------|-----------|----------------|-----------|----------------|---------------------|------------------------------|----------------------------------|
| A | {part number}     | {pkg}     | {LCSC C-number}| {qty}     | {$X.XX}       | {lib_id or "None"}  | {Vin, Vout, Iout, dropout}  | {pros and cons}                  |
| B | {part number}     | {pkg}     | {LCSC C-number}| {qty}     | {$X.XX}       | {lib_id or "None"}  | {specs}                      | {pros and cons}                  |
| C | {part number}     | {pkg}     | {LCSC C-number}| {qty}     | {$X.XX}       | {lib_id or "None"}  | {specs}                      | {pros and cons}                  |

**Recommendation:** {which and why}

**Four-gate check for selected part:**
- [ ] Spec conformance — meets requirements {R#, R#}; **[CRITICAL] {R#}: {cited datasheet evidence}**. Spec-named candidates evaluated: {list / why each dropped}.
- [ ] Works — {brief confirmation of electrical fit}
- [ ] PCBway-sourceable — {distributor} {PN}, {qty} in stock; package {pkg} assembly-friendly
- [ ] KiCad ready — {lib_id} / {footprint} (verified on disk)

**User selection:** ___

---

## Role: {next role}

{Repeat the table above for each functional role: regulators, ICs, sensors, connectors, etc.}

---

## Passive Components

Prefer common E-series values from `parts.preferred`. Non-standard values (exotic value or tight tolerance PCBway may not stock) are flagged.

| Role                  | Value   | Package | Preferred? | Notes                            |
|-----------------------|---------|---------|------------|----------------------------------|
| Decoupling (per IC)   | 100nF   | 0805    | Yes        | Standard from preferences        |
| {specific passive}    | {value} | {pkg}   | {yes/no}   | {why this value — datasheet ref} |

---

## Connectors

| Role           | Part Number       | Type     | Distributor PN | Notes          |
|----------------|-------------------|----------|----------------|----------------|
| {role}         | {pn}              | {family} | {LCSC C-number}| {from prefs?}  |

---

**Status:** DRAFT — Awaiting user selection

**Next step:** Once all roles have selections, proceed to Stage 3 (BOM)
