# Analog Noise & Layout — Reasoning Guide

This document is the **reasoning layer** behind the skill's analog-noise checks
(`analyze_analog.py` at Stage 5, `analyze_pcb_si.py` at Stage 8). The automated
checkers flag *candidates*; this guide tells you (and Claude) **how to decide
whether a flag actually matters for a given design** — so we add the mitigations
that buy real performance and skip the ones that just cost board area.

> The single most important lesson, stated up front:
> **Most analog-noise problems are fixed at the schematic/BOM level, not in
> layout.** A missing input filter cap is a *schematic* defect — you cannot add a
> part during routing. Guard pours, the thing people reach for in layout, are
> usually the *least* important mitigation. Spend effort in proportion to payoff,
> and the payoff is front-loaded into the schematic.

---

## 1. The mental model: where does noise actually enter, and does it matter?

Before applying any rule, answer three questions about the signal:

1. **What is the signal bandwidth?** Not the ADC's sample rate — the *real*
   bandwidth of the thing you're measuring. A weigh scale, thermocouple, or
   strain gauge is **essentially DC** (sub-Hz to tens of Hz). A microphone or
   vibration sensor is audio-band. This sets which noise frequencies can land
   *in band*.

2. **What is the source impedance?** A load cell / Wheatstone bridge is
   **low impedance** (350 Ω or 1 kΩ typical). A pH probe, photodiode in
   photovoltaic mode, or electrometer input is **high impedance** (MΩ–GΩ).
   This is the single biggest factor in deciding whether **guarding** is worth
   anything (see §7).

3. **What is the resolution / gain?** A 24-bit ADC with a PGA at gain 128 is
   resolving sub-microvolt steps at the input. Tiny coupled noise that a 10-bit
   ADC would never see becomes a real error here. High resolution **raises the
   stakes** for every mitigation, but does **not** change *which* mitigation is
   most effective.

The answers reorder the priority list. For the canonical case this skill keeps
hitting — **a low-impedance bridge into a high-resolution Σ-Δ ADC** (load cells,
the NAU7802/HX711/ADS124x family) — the priority order is in §3.

---

## 2. Two facts about Σ-Δ ADCs that change everything

**(a) The measurement bandwidth is ~DC, and the modulator oversamples heavily.**
A delta-sigma ADC samples at a high modulator clock (hundreds of kHz–MHz) but
decimates down to a low output data rate (10–320 SPS for load-cell parts). The
huge oversampling ratio means **the on-chip digital filter removes almost all
high-frequency noise** for you. Consequences:

- A **simple first-order RC** is sufficient as the external anti-alias / EMI
  filter — you do not need a multi-pole active filter. Choose the differential
  RC −3 dB corner **10×–100× below the modulator frequency** for 20–40 dB of
  rejection at the aliasing bands. ([TI E2E FAQ][aaf], [Cadence][cad])
- Broadband thermal/HF pickup that lands *outside* the output bandwidth is
  filtered out. So 2.4 GHz WiFi does **not** directly corrupt a 10 SPS weight
  reading… *with one critical exception* — see §3.

**(b) The only thing the chip can't filter is what gets rectified before it.**
That exception is RF rectification (§3.2). High-frequency energy that reaches a
nonlinear junction (the PGA/ADC input) gets **demodulated into a DC offset**
that lands squarely in your measurement band. The digital filter cannot remove
it because by then it *is* a DC error. This is why a board can pass on the bench
and drift the moment the radio transmits.

---

## 3. The priority ladder (low-Z bridge → high-res Σ-Δ ADC)

Apply mitigations in this order. Each is rated by payoff and by *where it lives*.

| # | Mitigation | Lives in | Payoff | Notes |
|---|-----------|----------|--------|-------|
| 1 | **Input RC filter at the ADC inputs** (differential + common-mode) | Schematic/BOM | ★★★★★ | Anti-alias *and* the primary defense against RF rectification. The #1 thing. |
| 2 | **Ratiometric reference** (excitation = ADC reference) | Schematic | ★★★★★ | Cancels excitation noise & drift *for free*. Often already inherent to the part's app circuit. |
| 3 | **Differential routing symmetry** (pair routed together, equal length, same layer) | Layout | ★★★★☆ | Free common-mode rejection of coupled noise (incl. the radio). Costs only discipline. |
| 4 | **Solid reference plane under the signal** (don't cross plane splits) | Layout/stackup | ★★★★☆ | You usually already have a GND plane. Keep analog on the GND-referenced layer. |
| 5 | **Local supply/reference decoupling** (AVDD, REF, PGA bypass) | Schematic/BOM | ★★★★☆ | Per datasheet. Cheap, high value, easy to forget. |
| 6 | **Placement**: amp close to the connector; analog away from RF/switching/digital aggressors | Layout | ★★★☆☆ | Shortens the vulnerable run; reduces coupling at the source. |
| 7 | **Shield pour between an aggressor and the analog run** (e.g., GND on the layer facing the radio) | Layout | ★★★☆☆ | Targeted, not board-wide. Worth it *specifically* under an RF/switching part. |
| 8 | **Guard ring / guard trace around the sensitive node** | Layout | ★☆☆☆☆ *(for low-Z)* / ★★★★☆ *(for high-Z)* | **Impedance-dependent.** Near-useless for a low-Z bridge; essential for a high-Z electrometer node. See §7. Do **not** cargo-cult this. |

**Read the table as a budget.** If a tight form factor forces a trade, you cut
from the **bottom**. Sacrificing #8 (guard pours) to keep your form factor is
almost always correct for a low-impedance bridge. Sacrificing #1 (input
filtering) is never correct.

---

### 3.1 Input filtering (mitigation #1) — the detail

The standard precision-ADC front end is a **differential RC** plus a
**common-mode RC**:

```
   IN+ ──[ Rs ]──┬───────────────┬──── ADC AIN+
                 │               │
              [ Ccm ]         [ Cdiff ]      Cdiff >> Ccm  (typ. 10×)
                 │               │
   IN- ──[ Rs ]──┴───────────────┴──── ADC AIN-
              [ Ccm ]
                 │
                GND
```

- **Cdiff** sets the differential bandwidth (the signal). With two series Rs:
  f_-3dB(diff) = 1 / (2π · 2Rs · Cdiff).
- **Ccm** to GND on each leg sets the common-mode bandwidth (rejects CM /
  RF pickup). Keep **Cdiff ≥ 10× Ccm** so that **mismatch in the two Ccm caps
  doesn't convert common-mode noise into differential error** — the whole point
  of the bridge is differential, so don't let the filter unbalance it.
- **Rs** small (100 Ω – 1 kΩ). Too large adds Johnson noise and interacts with
  the ADC's switched-cap input; too small does nothing. The RC also forms the
  EMI filter that stops RF rectification (§3.2).
- **NAU7802 specifics** (this skill's reference part): the datasheet
  characterizes the inputs with a **330 pF** filter cap at 3.3 V AVDD
  (**680 pF** at 4.5 V), and recommends a differential cap across an **unused**
  input pair with the PGA cap setting enabled. Honor the part's
  `LDOMODE`/PGA-bypass cap and AVDD decoupling from the datasheet.
  ([NAU7802 datasheet][nau], [SparkFun Qwiic Scale][spark])

If the netlist shows an `analog_differential` input that goes **straight from
the connector to the ADC pin with nothing on it**, that is the defect to flag at
Stage 5 — exactly the failure mode that motivated these checks.

### 3.2 RF rectification (why HF matters even at DC) — the detail

Out-of-band RF (a 2.4 GHz radio, a switching regulator's edges) couples into the
amplifier's input/supply/output pins and is **rectified by the internal
junctions**, appearing as a **DC offset shift** at the output. Because it's now
DC, it sits in-band and the Σ-Δ filter can't remove it; you see it as a baseline
shift or drift correlated with radio activity. ([ADI MT-096][mt096],
[TI SBOA128][sboa128])

Defenses, in order: **(1)** input RC/EMI filter (§3.1) — the dominant fix;
**(2)** keep the vulnerable trace short and away from the radiator (placement);
**(3)** a **shield pour** between the radiator and the trace. Note that #1 is a
schematic fix and does more than any copper. A part with a high **EMIRR** spec
already has internal input filtering and needs less external help.

### 3.3 Ratiometric reference (mitigation #2) — the detail

For a bridge sensor, derive the **ADC reference from the same source that
excites the bridge** (a 4-wire bridge: REF± wired to the excitation lines). The
ADC output then encodes V_in/V_ref, so **excitation voltage error, drift, and
noise cancel** — provided the reference and signal paths see the **same filter
bandwidth** so their noise stays correlated. This is essentially free accuracy
and is often already baked into the part's recommended circuit (e.g., NAU7802
AVDD = excitation = reference domain). Verify it's actually wired that way; don't
reference the ADC to a separate regulator if the part supports ratiometric.
([TI SBAA532][sbaa532], [TI SBAA154][sbaa154], [ADI AN-96][an96])

---

## 4. Reference planes & return current (mitigation #4)

Every trace's return current flows in the plane directly beneath it (lowest
inductance). Two rules follow:

- **Single ground plane, partitioned by placement — do not split the GND
  plane.** Modern consensus (Henry Ott) is that a split plane causes more
  problems than it solves: a trace crossing the split forces its return into a
  big loop that radiates and picks up noise. Instead use **one solid GND plane**
  and **partition the layout** so analog parts/traces sit over the analog region
  and digital over the digital region. ([Henry Ott][ott])
- **Keep sensitive analog on the layer that references GND.** On a 4-layer
  `Sig / PWR / GND / Sig` stack, the bottom signal layer references the solid
  GND plane while the top references the (often split) power plane. Route the
  sensitive analog on the **GND-referenced** layer, and **don't let it cross a
  power-plane split** (the return can't follow). A signal that must change layers
  needs its return able to follow — which on a power-vs-GND reference change can
  only happen through a nearby decoupling cap, so keep layer-change vias short
  and near decoupling.

---

## 5. Differential routing symmetry (mitigation #3)

A differential pair rejects whatever noise hits **both** legs equally (common
mode). That rejection is only as good as the **symmetry**:

- Route the two legs **together, same layer, equal length**, same number of
  vias. An asymmetry (one leg on F.Cu, the other dropping to B.Cu; or unequal
  length) unbalances the coupling and **converts common-mode noise into
  differential error** — defeating the bridge.
- This is *free* — it costs routing discipline, not parts or area — so it ranks
  high. The checker flags pairs that are split across layers or length-mismatched
  beyond a threshold.

---

## 6. Placement (mitigation #6)

- Put the amplifier/ADC **close to its sensor connector** so the unamplified,
  high-impedance-relative-to-noise signal travels as little distance as possible.
- Keep analog input traces **away from aggressors**: RF modules (antenna keep-out
  + lateral distance), switching regulators (the SW node and its loop), crystals,
  fast digital buses. A long analog run passing under/near a radio is the classic
  SNR killer.
- If the geometry forces the analog run near an aggressor (tight form factor),
  fall back to: shorten it, get it onto the GND-referenced layer, keep the diff
  pair tight, and add a **targeted** shield pour between them (§7) — in that order.

---

## 7. Guarding — when it's worth it, and when it isn't

A guard is grounded (or driven) copper around a sensitive node that intercepts
**leakage current** and **capacitively-coupled noise** before it reaches a
high-impedance input.

**The decision rule is source impedance:**

- **High-impedance node (≥ ~1 MΩ): guarding is essential.** Leakage currents
  across the board surface develop real error voltages into a high impedance, and
  a guard held at the node potential nulls the driving voltage for that leakage.
  This is the electrometer / pH-probe / photodiode-TIA case. For best effect the
  guard is **driven** from a low-impedance buffer at the node's potential, and
  must be a **continuous ring with no gaps**. ([Cadence guard ring][guard],
  [Microchip leakage][leak])
- **Low-impedance node (a 350 Ω / 1 kΩ bridge): guarding buys little.** The
  source impedance is so low that surface leakage and the small capacitive
  coupling a guard addresses are negligible compared to the signal. Here, the
  **solid GND reference plane under the trace** (§4) plus **differential
  symmetry** (§5) already do the job. A guard ring is a *nice-to-have*, not a
  *must*, and **must not** be added at the cost of form factor, trace symmetry,
  or a continuous reference plane.

**If you do add guard/shield copper, do it right or not at all:**

- Tie it to GND at **both ends and stitch along its length** (≤ λ/20 of the
  highest noise frequency you care about — ~3 mm pitch under a 2.4 GHz radio,
  ~5 mm otherwise). A guard grounded at only one end is a floating stub that
  **radiates** — worse than nothing.
- Make it **continuous**. A pour shredded into islands by other routing provides
  no continuous return and its isolated islands can re-radiate. **Do not pour
  board-wide GND on signal layers if routing fragments it** — on a board that
  already has a solid internal GND plane, the outer-layer pour is *supplementary*,
  not your reference. Carve a clean lane for a *targeted* guard instead, or skip
  it.
- A guard that pinches one leg of a diff pair more than the other **unbalances
  the pair** (§5). Keep clearances symmetric.

---

## 8. Worked example — the DualScale load-cell board

Concrete application of the ladder, to show the reasoning (this is the board that
motivated the checks):

- **Part/signal:** NAU7802 (24-bit Σ-Δ, PGA ≤128), dual load cells (low-Z
  bridges), ~10–80 SPS → **DC-band, low-Z, high-resolution**. Use the §3 ladder.
- **Aggressor:** an ESP32-S3 (2.4 GHz WiFi) sitting over the board; a long
  analog input pair runs under it.
- **What actually mattered:**
  1. **Input filtering was *missing*** — LC signals ran connector→ADC pin bare.
     This is the real defect (ladder #1) and a *schematic* fix: add differential
     + CM caps (≈330 pF-class Cdiff at 3.3 V) and small series R at the inputs.
  2. **Ratiometric** (#2) — AVDD = excitation = reference domain; verify wired so
     excitation noise cancels.
  3. **Diff symmetry** (#3) — one pair had a leg on F.Cu and the other dropping
     to B.Cu (asymmetric vias). Reroute together on the GND-referenced layer.
  4. **Reference plane** (#4) — solid In2 GND plane already present; keep the LC
     pairs on the B.Cu (GND-referenced) layer, off the split power plane.
  5. **Guarding** (#8) — the bridge is **low-Z**, so guard pours are the *lowest*
     priority. Board-wide outer pours fragmented into islands by routing → drop
     them. The **one** worthwhile piece of copper is a **targeted F.Cu shield**
     directly under the ESP32 over the analog corridor (#7), stitched to GND.
- **Verdict:** the expensive, area-hungry mitigation (guards) was the *least*
  important; the cheap schematic mitigation (input caps) was the *most*
  important and was the thing actually wrong. That asymmetry is the whole point
  of this guide.

---

## 9. Quick decision checklist (what the analyzers key off)

For every net classed `analog` / `analog_differential` / `high_impedance`:

- [ ] **Input filter present?** Differential cap (and CM caps for differential
      inputs) at the ADC/amp input, with series R. *(Stage 5, blocking for
      sensitive inputs.)*
- [ ] **Cdiff ≥ ~10× Ccm**, and CM caps matched, so the filter doesn't unbalance
      the pair.
- [ ] **Ratiometric reference** wired (bridge sensors): REF derived from
      excitation.
- [ ] **Supply/reference/PGA decoupling** per datasheet present.
- [ ] **Differential pair symmetric** in layout: same layer, ~equal length,
      ~equal via count. *(Stage 8.)*
- [ ] **On the GND-referenced layer**; does **not** cross a power-plane split.
      *(Stage 8.)*
- [ ] **Run length** reasonable and **clear of RF/switching/digital aggressors**;
      amp near its connector. *(Stage 8.)*
- [ ] **Guarding** considered **only by source impedance**: required for
      high-Z, optional/advisory for low-Z. If added: continuous, GND both ends,
      stitched, symmetric. **Never** at the cost of #1–#4. *(Stage 8, advisory.)*

---

## Sources

- [TI E2E — Delta-sigma ADC anti-aliasing filter component selection (FAQ)][aaf]
- [Cadence — Delta-Sigma ADC design][cad]
- [Nuvoton NAU7802 datasheet][nau]
- [SparkFun Qwiic Scale (NAU7802) hookup][spark]
- [ADI MT-096 — RFI Rectification Concepts][mt096]
- [TI SBOA128 — EMI Rejection Ratio (EMIRR) of op amps][sboa128]
- [TI SBAA532 — A Basic Guide to Bridge Measurements][sbaa532]
- [TI SBAA154 — Load Cell Output / Excitation white paper][sbaa154]
- [ADI AN-96 — Delta-Sigma ADC Bridge Measurement Techniques][an96]
- [Henry Ott — Grounding of Mixed-Signal PCBs (single GND plane)][ott]
- [Cadence — What is a Guard Ring and How to Design It][guard]
- [Microchip Developer Help — Leakage Currents][leak]

[aaf]: https://e2e.ti.com/support/data-converters-group/data-converters/f/data-converters-forum/955466/faq-delta-sigma-adc-anti-aliasing-filter-component-selection
[cad]: https://resources.pcb.cadence.com/blog/2023-delta-sigma-adc
[nau]: https://www.digikey.com/htmldatasheets/production/1340443/0/0/1/nau7802-datasheet.html
[spark]: https://learn.sparkfun.com/tutorials/qwiic-scale-hookup-guide/all
[mt096]: https://www.analog.com/media/en/training-seminars/tutorials/MT-096.pdf
[sboa128]: https://www.ti.com/lit/an/sboa128a/sboa128a.pdf
[sbaa532]: https://www.ti.com/lit/an/sbaa532a/sbaa532a.pdf
[sbaa154]: https://www.ti.com/lit/wp/sbaa154/sbaa154.pdf
[an96]: https://www.analog.com/en/resources/app-notes/an-96fa.html
[ott]: https://hott.shielddigitaldesign.com/techtips/split-gnd-plane.html
[guard]: https://resources.pcb.cadence.com/blog/2019-what-is-a-guard-ring-and-how-to-design-it-properly
[leak]: https://developerhelp.microchip.com/xwiki/bin/view/products/amplifiers-linear/operational-amplifier-ics/precision-design/leakage-currents/
