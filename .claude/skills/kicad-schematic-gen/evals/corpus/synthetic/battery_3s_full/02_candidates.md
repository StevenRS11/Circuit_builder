# Component Candidates — battery_3s

> **FINAL UPDATE (post-delivery due diligence): switched to Path A' — MAX77961.**
> A pre-routing re-sweep of the charger market found the **Analog Devices MAX77961** — a single-chip
> 2S/3S buck-boost charger with an *integrated* system power-path (instant-on with dead/absent
> battery), autonomous resistor-set config (no I2C), and on-chip NTC — which the original Stage-2
> research missed. It collapses the entire Path B charger + ideal-diode MUX (BQ24650 + LM5050-1 +
> LTC4412 + 4 power FETs + sense + inductor) into one IC. R2/R11 re-verified answer-blind; R7
> (USB back-feed) is handled by the MAX77961 CHGIN "true load disconnect" with OTG disabled — an
> external reverse-block FET (Q2) was trialed then removed as non-functional/redundant. Selected and
> built as the final design. The Path B analysis below is retained for the record.



> 2-3 candidates per role with tradeoffs. **User picks the winner for each role before the BOM is finalized.**
> Generated Stage 2 via per-role answer-blind research subagents. Datasheets + fact cards cached in `battery_3s_datasheets/`.

## Selection Criteria

PCBway turnkey (LCSC → DigiKey → Mouser). Four gates per part: **Spec conformance** (datasheet-cited for every [CRITICAL]), **Works**, **PCBway-sourceable** (in-stock + assembly-friendly package), **KiCad ready** (built-in symbol+footprint).

---

## Role: USB-C PD Sink Controller (R1, R7)

**Requirements:** Negotiate a **fixed 15V** PD profile passively (no MCU); hold off rather than false-fall-back to 5V if the source can't supply 15V; expose a power-good/enable to drive the external VBUS blocking FET (R7).

| # | Part | Package | LCSC PN | Stock | Price | KiCad Lib? | 15V select | Key Specs / Tradeoffs |
|---|------|---------|---------|-------|-------|-----------|-----------|----------------------|
| **A ✅** | **CH224K** | SSOP-10-EP | C970725 | ~6,654 | $0.29 | `Interface_USB:CH224K` ✓ | 56 kΩ CFG1→GND (defined level) | Native KiCad symbol; resistor-strap 15V (most deterministic); PG open-drain only (no internal gate driver — relies on external R7 FET, which is fine since that FET is sourced anyway). **Recommended.** |
| B | IP2721 | TSSOP-16 | C603176 | ~100k | $0.38 | `Interface_USB:IP2721` ✓ | SEL pin **floating** = 15V | Integrated VBUSG gate driver (drives external N-MOS, nice for R7). But 15V = a *floating* pin (least robust for "no false fallback"); coarse 3-level only. Good documented 2nd choice. |
| C | HUSB238 | DFN-10 | C7471904 | ~1,174 | $0.23 | **None** ✗ | 14 kΩ VSET→GND | Cheapest + resistor-defined + gate output, but **no KiCad symbol** (authoring friction), single-source, low stock, DFN-10 pinout unverified. **Rejected on KiCad-ready gate.** |

**Recommendation: CH224K.** Only candidate that is simultaneously multi-source in stock AND native KiCad symbol+footprint. Resistor-strapped 15V is the most robust against R1's "no false 5V fallback."

**Four-gate check (CH224K):**
- [x] Spec conformance — R1: 56kΩ strap requests 15V only; PG asserts only when 15V is actually present (no silent 5V path). R7: PG enables external blocking FET.
- [x] Works — passive PD sink, standard trigger-board topology.
- [x] PCBway-sourceable — LCSC C970725, ~6.6k stock, SSOP-10-EP standard SMT.
- [x] KiCad ready — `Interface_USB:CH224K` / `Package_SO:SSOP-10-1EP_3.9x4.9mm_P1mm_EP2.1x3.3mm` (verify EP→GND in Stage 4).

**User selection:** ___ (recommend CH224K)

---

## Role: 3S Charger + System Power Path (R2 [CRITICAL], R3, R4, R5, R10) — ⚠️ ARCHITECTURE DECISION

**Requirements:** R2 [CRITICAL] SYS live whenever VBUS present regardless of battery; R3 charge to 12.2V; R4 ~1.5A, resistor-set, **MCU-free preferred** ("I2C acceptable only if no hardware-only power-path option fits"); R5 NTC temp-qualified charging; R10 expose CHG + PG status.

### Decisive research finding
**No single part combines (NVDC system power path) + (3S) + (hardware-only/resistor-set config).** The field splits cleanly:
- **Integrated-NVDC parts** (BQ25713, BQ25703A) → **pass R2**, but are **I2C-only** (no resistor-set) and have **no TS pin** (R5 must move off-chip).
- **Resistor-set standalone chargers** (BQ24650, BQ25756) → MCU-free with a TS pin, but their converter output **is the battery** — **no SYS rail** → **fail R2**.

This forces a fork. Two valid architectures:

### Path A — Integrated NVDC charger (spec's named approach)

| # | Part | Package | LCSC PN | Stock | Price | KiCad | R2 | Notes |
|---|------|---------|---------|-------|-------|-------|----|----|
| **A1** | **BQ25713** (RSNR) | QFN-32-EP 4×4 | C2878935 | 2,600 | $1.13 | None (custom) | ✅ **satisfied** (answer-blind verified) | Buck-boost NVDC **controller**; "Instant-on with no battery"; VSYS pin held from VBUS. Pin-compatible with BQ25703A. |
| A2 | BQ25703A (RSNR) | VQFN-32-EP 4×4 | C188229 | 6,269 | $1.42 | None (custom) | ✅ satisfied (same family) | Drop-in 2nd source; higher stock, pricier. |

**Path A consequences (all forced by R2):**
- **R4 not met** — charge V/I are **I2C-register-set**; needs the downstream ESP32 to configure charging over I2C. (Permitted by R4's escape clause; SYS still comes up instant-on so the ESP32 can boot first.)
- **R5 needs off-chip** — no TS pin. Options: ESP32 reads the NTC on its own ADC and gates charging over I2C, **or** an external window-comparator gates CE. Added firmware/circuitry.
- **External power stage** — it's a controller: needs 4 external N-FETs (Q1–Q4), inductor, sense resistors → larger, more complex board.
- **Custom 32-pin QFN symbol** must be authored (Stage 6).
- R3 (12.2V): set via `MaxChargeVoltage()` register. R10: CHRG_OK pin + I2C STAT (no discrete CHG-LED pin).

### Path B — Standalone resistor-set charger + discrete power-path ORing ✅ SELECTED

Decouple the two functions: a **standalone resistor-set 3S charger** charges the battery from VBUS (MCU-free, **keeps R4 + R5**), and a separate **power-path ideal-diode ORing** (VBUS ∨ VBAT → SYS) makes SYS live whenever VBUS is present (**meets R2's testable core**). SYS = max(VBUS, VBAT), exactly how the spec already describes VSTEPPER.

**Resolved topology (sourced + R2/R11 re-verified):**

| Block | Part | LCSC PN | KiCad | Role / verification |
|-------|------|---------|-------|---------------------|
| Charger | **BQ24650** (RVAR) | C53712 | `Battery_Management:BQ24650` ✓ | Sync-buck **controller**. R3: VFB 2.1V → 12.2V via 481k/100k divider. R4: ICHG via 25mΩ sense (40mV FS → 1.6A, regulates to 1.5A), MCU-free. R5: **TS pin** on-chip (NTC + VREF divider). R10: STAT1/STAT2 open-drain. MPPSET divider set >1.2V at 15V to keep MPPT out of the way. |
| MUX — battery branch | **LTC4412** + P-FET (AO4407A) | C514442 | `Power_Management:LTC4412xS6` ✓ | Low-IQ PowerPath ideal diode (**11µA TYP / 19µA MAX**, CTL shutdown). Auto-disconnects battery when VBUS raises SYS. **This is what makes Path B pass R11.** |
| MUX — VBUS branch | **LM5050-1** + N-FET (AO3400A / CSD18540Q5B) | C129323 | `Power_Management:LM5050-1` ✓ | Active ideal diode, ~25ns reverse comparator. **Blocks SYS→VBUS back-feed → protects CH224K (your point 1) and is the R7 reverse-block** (folds in the separate R7 FET). CH224K VBUS taps the USB-connector side, upstream of this diode. |

**Why LTC4412 (not dual-LM5050-1) on the battery branch — R11 [CRITICAL] re-verified for Path B:** the LM5050-1 battery branch left powered draws ~100µA TYP / 147µA MAX IVS (its OFF pin does *not* stop IVS), so dual-LM5050-1 **fails** R11 (~117µA TYP). LTC4412 at 11µA IQ → storage budget **LTC4412 11µA + BQ24650 sleep 15µA + ~2µA leakage ≈ 28µA TYP / 36µA MAX < 100µA ✓** (answer-blind verified, BQ24650 SLEEP IBAT p.7 §7.5; LTC4412 IQ datasheet front-page + EC). LM5050-1 stays on the VBUS branch where its IQ is irrelevant in storage (USB unplugged → unpowered).

**Master switch wiring (your point 2):** the SPDT (C&K OS102011MS2QN1) drives a SYSTEM_EN control net that cuts **both** outputs — AP63200 EN pulled low (5V off) + a stepper-output high-side load-switch P-FET (AO4407A) off. SYS/MUX stay alive at µA with no load; both output rails dead in storage.

**Path B consequences:** all-hardware charging (no I2C/firmware), charger + both MUX controllers have KiCad symbols, on-chip NTC (R5). Cost vs Path A: an extra MUX block (2 controllers + 2 FETs) and the charger's external sync-buck power stage (2 N-FETs + 10µH inductor + 25mΩ sense). All discretes sourced (see Passives below).

### Rejected / dropped (charger)
- **BQ24650 as the *power-path* charger** — ❌ **R2 FAIL** (disqualified for Path A use): datasheet p.1 "**Stand-Alone** Synchronous Buck Battery Charge Controller… for Solar"; no SYS/VSYS pin, output node is the battery. *(This is exactly the prior wrong pick the spec revision called out. It is only viable inside Path B paired with a separate ORing MUX.)*
- **BQ25756** — ❌ R2 FAIL: "Battery Charge Controller," power-path manager is discharge-loads-only (16mA), no SYS pin.
- **MP2731 (1S), MP2762A (2S)** — dropped on cell count (cannot charge 3S).

**Decision: Path B — SELECTED by user (2026-05-30).** All-hardware charging (R4/R5 kept), R2 met by the ideal-diode ORing MUX, R11 met by LTC4412's low IQ.

**Four-gate check (Path B charger BQ24650 + MUX):**
- [x] Spec conformance — R2: VBUS-branch ideal diode holds SYS from VBUS with battery absent (LM5050-1 §8.2.3 ORing, p.18). R3: 481k/100k → 12.2V. R4: 25mΩ → 1.5A, resistor-set MCU-free. R5: BQ24650 TS pin. R10: STAT1/2. R11: LTC4412 ≈28µA storage (verified).
- [x] Works — BQ24650 sync-buck CC/CV at 15V→12.2V/1.5A; ideal-diode OR for SYS = max(VBUS,VBAT).
- [x] PCBway-sourceable — BQ24650 C53712, LTC4412 C514442, LM5050-1 C129323, all in stock, standard SMT.
- [x] KiCad ready — BQ24650/LTC4412/LM5050-1 all have built-in symbols; FETs use generic symbols.

**User selection:** ✅ Path B (BQ24650 + LTC4412 + LM5050-1)

---

## Role: 5V Logic Buck Regulator (R9)

**Requirements:** Vin 9–15V (≤~18–20V abs-max margin), 5.0V @ ≥1.5A, integrated-FET synchronous preferred, PG LED on the rail.

| # | Part | Package | LCSC PN | Stock | Price | KiCad Lib? | Vin / Iout | Tradeoffs |
|---|------|---------|---------|-------|-------|-----------|-----------|-----------|
| **A ✅** | **AP63200WU-7** | TSOT-23-6 | C2071868 | 25,035 | $0.16 | `Regulator_Switching:AP63203WU` ✓* | 3.8–32V / 2.0A sync | Widest Vin (32V abs-max margin), 4.7µH (smallest power stage), cheapest, spread-spectrum. *Symbol is the fixed-3.3V sibling — wire FB(1) with external 52.3k/10k divider for 5.0V (Vref 0.8V). No PG pin → rail-tap LED. **Recommended.** |
| B | TPS54202H | SOT-23-6 | C134129 | >100k | $0.21 | `Regulator_Switching:TPS54202DDC` ✓ | 4.5–28V / 2.0A sync | TI, >100k stock, robust. Needs ~15µH (bigger BOM). Vref 0.596V → 73.2k/10k for 5.0V. Solid 2nd source. |
| — | AOZ1280CI | SOT-23-6 | C41240 | 23,818 | $0.32 | `Regulator_Switching:AOZ1280CI` ✓ | 3–26V / **1.2A** non-sync | Dropped: <1.5A, non-synchronous (needs Schottky). |
| — | MP2451 / RT8059 | — | — | — | — | None | Dropped: MP2451 0.6A; RT8059 is a 1A / 5.5V-max part (can't take 9–15V). |

**Recommendation: AP63200WU-7** (TPS54202H as drop-in 2nd source). 2A sync gives 33% headroom at 1.5A; 32V abs-max is comfortable over the 15V rail. Both lack a PG pin → 5V power-good LED is a simple R+LED rail tap.

**Four-gate check (AP63200WU-7):**
- [x] Spec conformance — R9: 5.0V @ 2A from 9–15V, big Vin margin.
- [x] Works — synchronous buck, FB divider for 5.0V, 4.7µH.
- [x] PCBway-sourceable — LCSC C2071868, 25k stock, TSOT-23-6.
- [x] KiCad ready — family symbol `Regulator_Switching:AP63203WU` / `Package_TO_SOT_SMD:TSOT-23-6` (wire FB adjustable in Stage 6).

**User selection:** ___ (recommend AP63200WU-7)

---

## Role: Power FETs — Battery Reverse-Polarity (R6) + USB-C Reverse-Blocking (R7)

**Requirements:** P-FET ideal-diode on 3A paths; Vds ≥30V (2× over 3S 12.6V); low Rds(on); gate within ±20V.

| # | Part | Package | LCSC PN | Stock | Price | Symbol/Footprint | Specs | Use |
|---|------|---------|---------|-------|-------|------------------|-------|-----|
| **A ✅** | **AO4407A** | SO-8 | C16072 | 21,150 | $0.18 | `Device:Q_PMOS_GSD` / `Package_SO:SOIC-8_3.9x4.9mm_P1.27mm` | −30V, −12A, Rds(on) <17mΩ@−10V, Vgs ±25V, IDSS −1µA | **Both R6 & R7** (one P-FET family). Few-mV drop at 3A, runs cool. **Recommended.** |
| B | AO3401A | SOT-23 | C15127 | huge | $0.03 | `Transistor_FET:AO3401A` / `Package_TO_SOT_SMD:SOT-23` | −30V, −4A, ~50mΩ, Vgs ±12V | Light-path alt; ~0.45W at 3A in SOT-23 runs hot, Vgs margin tight. |
| — | SI2301CDS | SOT-23 | — | — | — | — | **Vds −20V only** | Rejected: fails ≥30V / 2× margin. |

**Recommendation: AO4407A** for both R6 and R7 (BOM consolidation; comfortable thermals at 3A).

**User selection:** ___ (recommend AO4407A ×2)

---

## Role: Master Power Switch (R11 [CRITICAL]) — EXTERNAL, via connector

**Requirements:** Disconnect both outputs in storage with **standby < ~100µA**. **Per user: the switch is supplied + mounted off-board; the PCB exposes a 2-pin SMD connector and does the load-switching on-board.**

| # | Part | Package | LCSC PN | Stock | Price | Symbol/Footprint | Topology |
|---|------|---------|---------|-------|-------|------------------|----------|
| **A ✅** | **J7 — JST-PH 2-pin SMD** (to external SPST) | SMD | C295747 | ~96k | $0.12 | `(connector)` / `Connector_JST:JST_PH_S2B-PH-SM4-TB_1x02-1MP_P2.00mm_Horizontal` | The external SPST toggles the **SYSTEM_EN** control line (µA only). On-board, SYSTEM_EN gates the AP63200 EN + the stepper load-switch P-FET (Q6) so **both** outputs cut. SYSTEM_EN default-low (R19) → open/unplugged switch = safe OFF. The 3A load stays on-board (Q6); no current flows in the external wiring. |

**R11 standby budget (answer-blind verified):** BQ25713 low-power mode (POR default, no I2C needed) = **22µA typ / 45µA max** + ~2µA P-FET leakage → **~24µA typ, <55µA worst** < 100µA ✓. Switch carries only enable/gate µA, never the 3A load. *(Note carried to Stage 4/6: no other always-on battery-rail bleeders permitted in storage.)*

> If Path B is chosen, the same switch gates the BQ24650 CE + output FET gates; budget recomputes against BQ24650's standby Iq (re-verify in Stage 4).

**User selection:** ___ (recommend C&K OS102011MS2QN1)

---

## Discrete power / magnetics / protection (Path B — all sourced)

| Role | MPN | Pkg | LCSC PN | Stock | Price | KiCad sym / footprint | Spec |
|------|-----|-----|---------|-------|-------|----------------------|------|
| Charger HS+LS FETs; VBUS-branch ideal-diode FET (primary) | AO3400A | SOT-23 | C20917 | 1.02M | $0.083 | `Transistor_FET:AO3400A` / `SOT-23` | 30V, 5.7A, 28mΩ |
| VBUS-branch FET upsize (if 3A continuous) | CSD18540Q5B | SON 5×6 | C86513 | ok | $0.90 | `Transistor_FET:CSD18540Q5B` / `TDSON-8` | 60V, 2.2mΩ |
| Reverse-protect / load-switch P-FETs (R6 + stepper LS + MUX bat) | AO4407A | SO-8 | C16072 | 21k | $0.18 | `Device:Q_PMOS_GSD` / `SOIC-8` | −30V, −12A, 17mΩ |
| Charger inductor (BQ24650 buck) | CYA0630-10UH | 7.2×6.6 | C5189958 | 75k | $0.16 | `Device:L` / `Inductor_SMD:L_7.3x7.3_H3.5`† | 10µH, 5.5A sat |
| Buck inductor (AP63200) | CYA0630-4.7UH | 7.2×6.6 | C5189748 | 43k | $0.17 | `Device:L` / `Inductor_SMD:L_7.3x7.3_H3.5`† | 4.7µH, 6.5A sat |
| Charge sense R | HoYLR2512-2W-25mR-1% | 2512 | C5375424 | ok | $0.03 | `Device:R` / `R_2512_6332Metric` | 25mΩ, 2W, 1% |
| USB ESD (VBUS+CC/data) | USBLC6-2SC6 | SOT-23-6 | C2827654 | ok | $0.02 | `Power_Protection:USBLC6-2SC6` / `SOT-23-6` | 3.5pF, VBUS+2 lines |
| Balance-tap / VBUS TVS | SMF5.0CA/12CA/15CA | SOD-123FL | C364279… | ok | $0.01 | `Device:D_TVS` / `D_SOD-123F` | per-tap standoff |
| 3S balance connector | B4B-XH-A | JST-XH 1×4 THT | C144395 | 272k | $0.03 | `Connector_Generic:Conn_01x04` / `JST_XH_B4B-XH-A_1x04_P2.50mm_Vertical` | 4P 2.5mm |

† No exact 7.2×6.6mm KiCad land — nearest generic `L_7.3x7.3_H3.5`; verify/author pad at layout.

## Standard passives (preferences.yaml)

| Role | Value | Package | Notes |
|------|-------|---------|-------|
| Decoupling (per IC) | 100nF | 0805 | Standard |
| Bulk on rails (VBUS/SYS/BAT) | 10µF | 0805 | Standard |
| Buck input cap | 10µF | 0805 | AP63200 Cin |
| Buck output cap | 2×22µF | 0805 | AP63200 Cout |
| Buck BST cap | 100nF | 0805 | SW→BST |
| Buck FB divider | 52.3k / 10k | 0805 | 5.0V from 0.8V Vref |
| Charger VFB divider | 481k / 100k | 0805 | 12.2V from 2.1V Vref |
| Charger MPPSET divider | set V>1.2V@15V | 0805 | disable solar MPPT loop |
| Charger BTST/REGN/VREF caps | 100nF / 1µF | 0805 | per BQ24650 datasheet |
| TS NTC divider | per JEITA window | 0805 | R5 |
| LED resistors | 1k | 0805 | power/charge LEDs |
| NTC thermistor | 10k (103AT) | 0805 | R5 pack temp, near battery conn |

## Connectors (preferences.yaml unless noted)

| Role | Part | Type | Source | Notes |
|------|------|------|--------|-------|
| USB-C PD input | USB4125-GF-A | USB-C | prefs | GCT mid-mount |
| Battery main | S2B-PH-K-S | JST-PH 2-pin | prefs | B+, B− |
| Battery balance | B4B-XH-A | JST-XH 4-pin | C144395 | B−, B1, B2, B+ (tap only) |
| VSTEPPER output | S2B-PH-K-S | JST-PH 2-pin | prefs | VSTEPPER, GND |
| 5V output | S2B-PH-K-S | JST-PH 2-pin | prefs | +5V, GND |
| Status header | 1×3 pin header | 2.54mm | std | CHG, PG, GND |

## LEDs (preferences.yaml)

| Role | Color | Package | Notes |
|------|-------|---------|-------|
| Power (VSTEPPER live ≥6V) | Green | 0805 | Vf 2.2 |
| Charging (STAT active) | Red | 0805 | Vf 2.0 |

---

## Final Selections Summary (Path B)

| Role | Selected part | LCSC PN | KiCad symbol |
|------|---------------|---------|--------------|
| PD sink | CH224K | C970725 | `Interface_USB:CH224K` |
| Charger | BQ24650 | C53712 | `Battery_Management:BQ24650` |
| MUX battery branch | LTC4412 (+AO4407A) | C514442 | `Power_Management:LTC4412xS6` |
| MUX VBUS branch | LM5050-1 (+AO3400A) | C129323 | `Power_Management:LM5050-1` |
| 5V buck | AP63200WU-7 | C2071868 | `Regulator_Switching:AP63203WU` |
| Reverse-protect / load-switch P-FETs | AO4407A | C16072 | `Device:Q_PMOS_GSD` |
| Charger/MUX N-FETs | AO3400A (CSD18540Q5B option) | C20917 | `Transistor_FET:AO3400A` |
| Master switch | EXTERNAL (user-supplied) via J7 JST-PH 2-pin SMD | C295747 | `(connector)` |

**Status:** ✅ SELECTIONS LOCKED (Path B). Proceed to Stage 3 (BOM + sourcing sheet + requirements coverage).
