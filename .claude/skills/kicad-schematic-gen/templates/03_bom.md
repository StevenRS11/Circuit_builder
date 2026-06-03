# Bill of Materials — {PROJECT_NAME}

> Finalized parts list. This is the single source of truth for the schematic build,
> **and** the data PCBway sources against — every line needs a distributor PN.
>
> **For ICs:** Use the `lib_id` and `footprint` from `check_kicad_library.py --lookup` (captured in Stage 2).
> **For passives:** Look up the package in `footprint_map.yaml` (e.g., `capacitors["0805"]`).
> **For preferred parts:** Use footprints/PNs from `preferences.yaml` directly.
> **Manufacturer:** the part's maker (e.g. Murata, YAGEO, Diodes Inc) — PCBway's BOM form requires it, and it travels with the MPN. For generic passives use your standard passive vendor (e.g. YAGEO). **Keep `Manufacturer` + `Part Number` together — never put a distributor code (LCSC `C…`) in the Part Number column; that goes in Supplier PN.**
> **Supplier PN:** capture an in-stock distributor PN (LCSC C-number preferred) for every line.

## BOM

| Ref   | Value        | Manufacturer | Part Number     | Description                  | Package  | KiCad Symbol (lib_id)                    | Footprint (KiCad)                        | In Stock? | Qty | Supplier  | Supplier PN   | Unit Price | Notes        |
|-------|-------------|--------------|-----------------|------------------------------|----------|------------------------------------------|------------------------------------------|-----------|-----|-----------|---------------|------------|--------------|
| U1    | {value}     | {mfr}        | {mpn}           | {IC fn, e.g. IC LDO 3.3V}    | {pkg}    | {Library:SymbolName}                     | {Library:FootprintName}                  | {yes/no}  | 1   | {supplier}| {pn}          | {$X.XX}    | {notes}      |
| C1    | {value}     | {mfr}        | {mpn}           | CAP CER {val} {V} {diel} {pkg}| {pkg}   | Device:C                                 | {footprint}                              | {yes/no}  | 1   | {supplier}| {pn}          | {$X.XX}    | {notes}      |
| R1    | {value}     | {mfr}        | {mpn}           | RES {val} {tol} {pkg}        | {pkg}    | Device:R                                 | {footprint}                              | {yes/no}  | 1   | {supplier}| {pn}          | {$X.XX}    | {notes}      |
| J1    | {value}     | {mfr}        | {mpn}           | {connector description}      | {type}   | {Library:ConnectorName}                  | {footprint}                              | {yes/no}  | 1   | {supplier}| {pn}          | {$X.XX}    | {notes}      |

## Summary

- **Total unique parts:** {N}
- **Lines with a confirmed in-stock distributor PN:** {N} / {N}
- **Lines flagged by `check_pcbway.py`:** {N blocking, N cautions}
- **Estimated parts cost (PCBway sources all):** ${X.XX}

## PCBway Sourceability

All parts are sourced and assembled by PCBway turnkey. Before sign-off:

1. **Rubric check** — `python check_pcbway.py {project}_03_bom.md --json` → **0 blocking**; cautions reviewed.
2. **Live stock** — every distributor PN confirmed in stock (LCSC → DigiKey → Mouser) via web search.
3. **Sourcing sheet** — `python check_pcbway.py {project}_03_bom.md --sourcing-sheet -o {project}_03_sourcing.md` → the table to submit to PCBway.

| Ref | Part Number | Distributor | Distributor PN | Stock | Assembly Rating | Notes / flags |
|-----|-------------|-------------|----------------|-------|-----------------|---------------|
| {ref} | {mpn}     | {LCSC}      | {C-number}     | {qty} | {OK/CAUTION}    | {notes}       |

---

**Status:** FINAL — Approved by user

**Next step:** Proceed to Stage 4 (Implementation Reference)
