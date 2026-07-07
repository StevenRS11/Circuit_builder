---
name: kicad-schematic-gen
description: Generate KiCad .kicad_sch schematic files from natural-language circuit descriptions. Use this skill whenever the user asks to design a PCB module, create a schematic, generate a KiCad file, build a breakout board, make a power supply circuit, create a sensor board, or anything involving electronic circuit design and KiCad. Also trigger when the user mentions specific ICs (like LDOs, USB-C controllers, sensor ICs, battery chargers) and wants a board or schematic built around them. This skill researches datasheets, determines application circuits, resolves pinouts, and outputs valid .kicad_sch files ready to open in KiCad for physical layout.
---

# KiCad Schematic Generator

Generate production-ready `.kicad_sch` schematic files from natural-language board descriptions through a **10-stage collaborative workflow**. Each stage produces a document the user reviews before proceeding, catching errors early — before they're baked into copper. The last stage closes the loop with reality: bench results are harvested into permanent gates, verified pinouts, and (eventually) proven blocks.

## Design hierarchy (read this first — it governs every change to this skill)

This skill is built around one loop:

> **Claude generates context → verifies that context → authors from it → verifies what it authored with scripts.**

- **Scripts live at the *verify* steps** (and as the deterministic *assembler*, `generate_from_data.py`, which is driven entirely by Claude-authored data and only emits after its gates pass).
- **Claude lives at the *generate* and *author* steps** — research, topology, part selection, the netlist, and the layout (placement + symbols).
- **Never script the judgment.** If a proposed script would make a design decision (which pins go where, where parts sit, what a custom symbol looks like), it's in the wrong place — that is Claude's job, guided by the docs/templates/references here. A script that *checks* such a decision is welcome; a script that *makes* it is not.
- **Each stage verifies its output back against the validated requirements of prior stages.** The approved Stage-1 spec — and each later approved doc — is the *test suite* the rest of the build must pass, the way code must satisfy its tests and compile. Output drift and hallucination *will* happen; the guardrail is to re-check against those verified documents at every gate, not to trust the current step's own reasoning. A spec requirement flagged **[CRITICAL]** is a hard gate: a choice that fails it is rejected no matter how good it looks on price, stock, or convenience. (This is the rule that would have caught selecting a non-power-path charger against a spec that demanded one.)

Why this works, and why it should be preserved:
1. **Circuits and their requirements are well described in natural language** — Claude can reason about intent, topology, and part choice directly from the spec.
2. **Almost every artifact is plain text** (Markdown specs/BOM, YAML netlist/design/layout, S-expression `.kicad_sch`/`.kicad_pcb`) — so Claude can read, author, diff, and review every file, and scripts can deterministically parse and verify them.

The loop, mapped to the stages and scripts:

| Step | Who | Stages / scripts |
|------|-----|------------------|
| generate context | Claude (research) | Stage 1 spec, Stage 2 candidates, Stage 3 BOM, Stage 4 impl ref |
| verify context | scripts | `analyze_dc`, `analyze_analog`, `check_pcbway`, `check_kicad_library`, `lookup_pinout` — validate the **intent** before building |
| author | Claude | Stage 5b netlist; Stage 6 layout YAML (placements + custom symbols) |
| verify output | scripts | `validate_kicad_sch`, `verify_netlist`, `cross_check_bom`, and `generate_from_data`'s pre-flight + self-verify gates — validate the **artifact** |
| backstop | script | `analyze_pcb_si` on the routed `.kicad_pcb` |

Every script in `scripts/` is verify/analyze/lookup only, except the builder library (`generate_kicad_sch.py`) and the assembler (`generate_from_data.py`) — both of which only ever act on Claude-authored data. Keep it that way.

### Subagents and the datasheet cache

Subagents are how the loop's *verify* legs get an **independent** check. The main
thread can't honestly re-check its own reasoning — it's already invested in its
choices. A subagent's **isolated context literally can't see that reasoning**, so
its verdict is unbiased. Isolation is the feature. The rule:

- **Use subagents for the `research` leg and the *non-scriptable* `verify` legs** —
  Stage 2 per-candidate sourcing, the Stage 2 `[CRITICAL]` requirement check, the
  Stage 4 independent pinout re-derivation, and the Stage 7 structural design review.
  Hand each one **explicit, frozen inputs** (the approved prior-stage doc + the
  canonical datasheet) and **never the answer you're hoping for.**
- **Keep the `author` legs (Stages 5b/6) and every user gate on the main thread.**
  Authoring needs the full design context, and **subagents can't talk to the user**,
  so anything that pauses for sign-off is main-thread by nature.
- **Never wrap the deterministic scripts in a subagent** — call them directly.

**Honor what they report.** A verify subagent's verdict is authoritative within its
scope — **do not overturn a `fails`/disagreement with your own reasoning** (that bias is
exactly what it was spawned to bypass). A failing check has three responses only: fix
the frozen inputs and re-run, reselect/redesign, or surface the verdict + citation to
the user — never "proceed anyway." Persist each verdict + citation into the durable
artifact (traceability YAML / review doc), and a `satisfied` verdict never excuses
skipping the deterministic scripts. For research, **the returned rows are the candidate
set**: synthesize from them (don't re-run from memory), carry `MPN`/`distributor_pn`/
`lib_id`/`footprint`/`datasheet_path` forward *verbatim* into the BOM and traceability,
re-dispatch on any gap, and reuse the cached `datasheet_path` as the input to the
verify subagents.

**Datasheet cache + fact cards:** research stages save each canonical datasheet
**once** to `{outputs}/{project_name}_datasheets/{MPN}.pdf` (with an `index.md`
citation ledger), so every downstream verifier reads the *same* frozen artifact
instead of re-fetching. Alongside each, the research subagent writes a structured
**`{MPN}.facts.yaml` fact card** holding the part's *intrinsic* facts —
sourcing, `lib_id`, `footprint`, and the pinout (number/name/type) — **not** any
per-design connectivity/placement (that judgment stays Claude-authored). The card is
the authoritative source the intrinsic BOM/`symbols:` fields are copied from and
verified back against: the Stage 4 pinout re-derivation validates the card, and the
Stage 7 review checks the schematic's pins/footprints against it. Cards seed from /
get promoted into `pinouts/pinout_db.json`.

See **`references/subagents.md`** for the per-stage recipes (exact context to hand
each subagent, return schema, and answer-blind prompt skeletons) and the cache
mechanics.

## Stage Overview

```
User description
      │
      ▼
┌─────────────────────────┐
│ Stage 1: SPECIFICATION  │  ← Claude researches best practices, writes formal spec
│   User reviews & signs  │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 2: COMPONENT      │  ← Claude finds parts on DigiKey/LCSC, presents candidates
│   SOURCING              │     with tradeoffs. User picks winners per role.
│   User selects parts    │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 3: BOM            │  ← Finalized parts list with supplier PNs, stock, cost
│   User confirms         │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 4: IMPLEMENTATION │  ← Each IC: datasheet pinout, application circuit,
│   REFERENCE             │     connections, passive sizing. User verifies pinouts.
│   User verifies         │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 5: DC ANALYSIS    │  ← Automated: voltage dividers, LDO thermal, LED current,
│   (analyze_dc.py)       │     I2C rise times, current budgets, cap sizing.
│   Fix before building   │     Catches value errors before they become wires.
└────────────┬────────────┘
             │ ← Feeds corrections back into Stage 4 if problems found
             ▼
┌─────────────────────────┐
│ Stage 5b: NETLIST       │  ← Pin-level connection declarations in YAML.
│   (verify_netlist.py)   │     Separates "what connects" from "where it's drawn".
│   + analyze_analog.py   │     Gate: analog front-end completeness (filters,
└────────────┬────────────┘     decoupling, ratiometric) for sensitive designs.
             ▼
┌─────────────────────────┐
│ Stage 6: SCHEMATIC      │  ← Claude generates .kicad_sch from netlist + BOM + impl ref
│   GENERATION            │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 7: VERIFICATION   │  ← Automated validator + netlist verify + design review
│   User gets final file  │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 8: PCB LAYOUT     │  ← Claude parses .kicad_pcb, checks placement distances,
│   REVIEW                │     trace widths, thermal pads, hot loops, and
│   (analyze_pcb_si.py)   │     analog-noise/SI (diff pairs, refs, aggressors).
│   User fixes before fab │
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 9: PCBway UPLOAD  │  ← Generate BOM + gerbers + centroid from source.
│   PACKAGE               │     Structural gate + answer-blind live BOM verify
│   (bom_verify.py +      │     (catches wrong-part distributor codes) + DRC-gated
│    generate_*.py)       │     fab export → {project}/PCBway_uploads/.
└────────────┬────────────┘
             ▼
┌─────────────────────────┐
│ Stage 10: BRINGUP &     │  ← Bringup checklist authored at delivery; after bench
│   FIELD REPORT          │     time, field report + promotion ritual: failures →
│   (check_ledger.py)     │     encoded gates/checks, successes → blocks / pinout
│   The board meets       │     DB / eval anchors. Ledger: validated_boards.yaml.
│   reality               │
└─────────────────────────┘
```

**Key principle:** The user signs off at each gate. Do NOT skip stages or auto-advance without user approval. Present each stage's document, discuss, revise if needed, then proceed.

**Non-negotiable, every run — the spec is the test suite.** The Stage-1 **Requirements Checklist** is the test suite for the entire build; treat it the way code treats its tests. Every part selection and the final design is checked *back* against it, not just forward from the previous step:
- **[CRITICAL] requirements are hard gates.** A choice that fails one is rejected regardless of price, stock, package, or convenience — verified with **cited datasheet evidence** at the **Stage 2** selection gate, the **Stage 3** `check_requirements.py` coverage gate, and the **Stage 7** traceability pass.
- **Evaluate the candidates the spec names; never substitute a requirement away.** If you drop a candidate, say why — a confident rationale is not evidence; cite the datasheet.
- This holds no matter where a run starts (e.g. resuming at Stage 2 from an already-approved spec). **It never needs to be asked for in the prompt — it is the skill.** If you find yourself restating these rules to make them happen, they belong here instead.

---

## Before You Start: Load Preferences

**Always read `preferences.yaml`** in the skill directory before beginning any stage. It contains:

- **defaults** — Passive package size, footprints, standard cap/resistor values
- **power_rails** — Available voltages, preferred regulators
- **connectors** — Preferred families with specific part numbers and footprints
- **parts** — `preferred` (go-to part per role, reached for first) and `on_hand` (parts physically kept for consignment/rework)
- **assembly** — Assembler (PCBway), service mode (turnkey/consigned), distributor sourcing order, and the part-selection **rubric** thresholds
- **design_rules** — Power symbols vs labels, auto-decoupling, power LED

When making any design decision, check preferences first.

### Assembly model: PCBway turnkey

**Boards are ordered as fully assembled boards from PCBway, not populated in-house.** PCBway sources every part for you from authorized distributors (LCSC → DigiKey → Mouser) and assembles the board. There is **no in-house inventory to track** and — unlike JLCPCB — **no public PCBway parts catalog/API to query.** This shapes part selection in two ways:

1. **Choose PCBway-friendly parts** (Stage 2): standard, assembly-friendly packages that are in stock at a distributor PCBway sources from. The rubric below is the gate.
2. **Verify the BOM is buildable** (Stage 3): run `check_pcbway.py` for the deterministic package/format checks, and confirm live distributor stock per line via web search.

Prefer parts from `parts.preferred` and the `connectors`/`power_rails.preferred_regulators` lists, but treat them as suggestions — always re-confirm each is currently PCBway-sourceable. If the user requests a specific part, use it but run it through the same rubric.

Also read `references/kicad_format.md` for the S-expression format reference.

### Using your own libraries (user-supplied symbols & footprints)

The skill is **not limited to KiCad's built-in libraries.** `check_kicad_library.py`
discovers every library the user has registered and searches them all, returning the
**real `lib_id`** (with its true nickname) and the symbol's **actual pins**. Libraries
are discovered, in increasing precedence (later wins a nickname clash):

1. KiCad's built-in install (`<kicad_root>/symbols/*.kicad_sym`),
2. the **global** `sym-lib-table` / `fp-lib-table` in the KiCad config dir — where the
   consolidated **`Custom`** lib and one-off libs (e.g. `ESP32S3`) are registered,
3. the **project** `sym-lib-table` / `fp-lib-table` — pass `--project-dir {project_dir}`,
4. any library pointed at explicitly: `--sym-lib [NICK=]PATH` / `--fp-lib [NICK=]PATH`.

```bash
# see everything that will be searched
python check_kicad_library.py --list-libs --project-dir {project_dir}
# resolve a part against built-ins + user libs (incl. project)
python check_kicad_library.py NAU7802 --lookup --project-dir {project_dir}
# point at an arbitrary library the user hands you
python check_kicad_library.py MYPART --lookup --sym-lib MyLib=/path/to/My.kicad_sym
```

**Always pass `--project-dir {project_dir}`** when working inside a project so its local
`Library` resolves. When a part resolves from a user library (`from_user_library: true`),
its pins are authoritative: use the returned `lib_id` and pins verbatim and treat the
fact card's pinout as verified — there is no hallucination risk because you are reading
the actual symbol, not reconstructing it.

**To add a new vendor part** (SnapEDA / Ultra Librarian / manufacturer zip), use the
**`kicad-import-lib`** skill. It installs the symbol + footprint + 3D model into the
`Custom` libraries this resolver already searches; re-run the lookup afterwards and the
part is a first-class citizen.

---

## Stage 1: Specification

**Input:** User's natural-language description of what the board should do.

**What Claude does:**
1. Parse the user's request for functional requirements
2. Research best practices for this type of circuit via web search:
   - Common topologies and reference designs
   - Industry-standard approaches (e.g., USB-C PD needs CC resistors or a PD controller)
   - Known pitfalls for this circuit type
3. Fill out the specification template (`templates/01_specification.md`) covering:
   - Purpose and function
   - Input power source and voltage
   - Power rails needed (with current budgets)
   - Functional blocks with interfaces
   - External interfaces and connectors
   - Environmental/mechanical constraints
   - Design constraints from preferences.yaml
4. Distill a **numbered Requirements list** in the spec (`## Requirements Checklist`) — each a single, testable statement. Flag hard constraints **[CRITICAL]** (a violation must block the build — e.g. "SYS rail must be live whenever VBUS is present, even with the battery absent or UVLO'd"). This list is the *test suite* every later stage checks its output against, so write each item so it can be confirmed against a datasheet or the generated design.
5. List any **open questions** where the description is ambiguous

**What to discuss with the user:**
- Are the power budget estimates reasonable?
- Are the chosen interfaces correct (I2C vs SPI, UART baud, etc.)?
- Any functional blocks missing?
- Any constraints not mentioned (size, temp, cost target)?

**Output:** Completed `{project_name}_01_specification.md` — saved to outputs directory.

**Gate:** User approves the spec before proceeding to Stage 2.

---

## Stage 2: Component Sourcing

**Input:** Approved specification from Stage 1.

### PCBway Part-Selection Rubric

Because PCBway sources and assembles every part, **choosing parts PCBway can comfortably source and place is part of the design, not an afterthought.** Score every candidate against this rubric and prefer the parts that pass it. The same criteria are checked mechanically over the final BOM by `check_pcbway.py` in Stage 3 — selecting well here means a clean check later.

| Criterion | Prefer | Avoid / flag |
|-----------|--------|--------------|
| **Distributor availability** | In stock now at LCSC (cheapest, widest PCBway catalog), else DigiKey/Mouser, with healthy stock (≥ `assembly.rubric.min_stock_qty`) | Out of stock, back-order, or distributor PCBway doesn't source from |
| **Package** | Standard SMT: 0402–1206 passives, SOT-23, SOIC/TSSOP/MSOP, QFP, QFN/DFN (pitch ≥ 0.4mm) | 01005 (block), 0201 (caution), fine-pitch BGA/CSP, bare die, leadless exotic |
| **Mounting** | SMT both sides OK | Through-hole / connectors (PCBway charges per-pin for hand/wave solder — confirm intent) |
| **Lifecycle** | Active, multi-source | Obsolete/EOL/NRND, single-source/sole-source, allocated long-lead parts |
| **KiCad support** | Symbol + footprint in KiCad built-in libraries (`check_kicad_library.py`) | No symbol → must source from SnapEDA/manufacturer (friction) |
| **Value sanity (passives)** | Common E-series values from `parts.preferred` | Exotic values, tight tolerances PCBway may not stock |
| **Handling** | Standard | MSL 3+ moisture-sensitive (fine, but note it) |

**Rule of thumb:** if it's a common part that JLCPCB/LCSC stocks in quantity in a standard SMT package, PCBway sources it easily and cheaply. The more a part deviates from that, the more it costs and the more likely it stalls the order. When two parts are otherwise equal, pick the one with more distributor stock and a second source.

**Run sourcing as a per-role subagent fan-out.** Steps 1-6 below are research — many
web searches and library lookups per role — so dispatch **one `general-purpose`
subagent per functional role** and let it return only the distilled candidate rows,
keeping the search noise out of the main thread. Hand each subagent its role, the
**spec requirements that apply to that role** (verbatim, with `[CRITICAL]` flags), the
rubric above, the `parts.preferred`/connector/regulator lists, and
`assembly.distributor_order`. Each subagent **caches every serious candidate's
datasheet** to `{outputs}/{project_name}_datasheets/{MPN}.pdf` + `index.md` **and writes
its `{MPN}.facts.yaml` fact card** (intrinsic facts: sourcing + `lib_id`/`footprint` +
extracted pinout). The main thread collates the rows and presents them at the gate, and
carries intrinsic fields forward **from the cards**, not by retyping. See recipe A in
`references/subagents.md`.

**What Claude does (per role):**
1. For each functional role in the spec (regulators, ICs, sensors, connectors), identify **2-3 candidate parts**
2. Check `parts.preferred` (and the `connectors` / `preferred_regulators` lists) first — any matching part gets automatic candidate status, but still verify it's currently PCBway-sourceable
3. **For every candidate IC**, check KiCad library availability:
   ```python
   from check_kicad_library import lookup_part
   result = lookup_part("AP2112K-3.3", project_dir="{project_dir}")
   # result["found"] → True/False
   # result["lib_id"] → "Regulator_Linear:AP2112K-3.3"
   # result["footprint"] → "Package_TO_SOT_SMD:SOT-23-5"
   # result["footprint_exists"] → True/False
   # result["from_user_library"] → True if it came from one of YOUR libs
   # result["pins"] → [{number, name, type}, ...]
   ```
   Or from CLI: `python check_kicad_library.py <part_number> --lookup --json --project-dir {project_dir}`
   - The resolver searches **every library the user has registered** — KiCad's
     built-ins **and** the user's own (the consolidated `Custom` lib, per-part libs
     like `ESP32S3`, the project's `Library`, plus anything passed via `--sym-lib`).
     See **"Using your own libraries"** below.
   - If `found: true` and `footprint_exists: true` → part is ready to use. Record the `lib_id` and `footprint` **verbatim** (the `lib_id` carries the real nickname, e.g. `Custom:NAU7802`).
   - If `from_user_library: true` → the symbol *is* the authoritative pin source for that part; its `pins` may seed the fact card as `pinout_verified` (no datasheet re-derivation needed for pin number/name — see Stage 4).
   - If `found: false` → flag it. The user will need to source the symbol/footprint from SnapEDA, Ultra Librarian, or the manufacturer, then install it with the **`kicad-import-lib`** skill (which drops it into the `Custom` lib the resolver already searches). Re-run the lookup afterwards. Prefer parts already in a built-in **or** a user library.
4. Confirm stock at a distributor PCBway sources from, in `assembly.distributor_order` order, via web search:
   - `site:lcsc.com "{part number}"` (LCSC first — cheapest, widest PCBway catalog)
   - `site:digikey.com "{part number}" in stock`
   - `"{part number}" vs "{alternative}" comparison`
   - Capture the LCSC part number (e.g. `C51118`) where available — it's what PCBway sources against most cheaply.
5. For each candidate, capture:
   - Part number (MPN), package, price
   - **Distributor + distributor PN + stock** (LCSC PN preferred)
   - **PCBway-sourceability** per the rubric (package OK? in stock? active/multi-source?)
   - **KiCad library status** (built-in symbol Y/N, footprint Y/N)
   - Key specs relevant to the role
   - Pros and cons vs alternatives
6. Recommend a winner per role with reasoning
7. List passive components needed (prefer values from `parts.preferred`, flag non-standard values)
8. List connectors (check the `connectors` list in preferences.yaml)

**Four-gate check for every selected part:**
- [ ] **Spec conformance** — satisfies every applicable Stage-1 requirement, with a **datasheet citation for each [CRITICAL] one**. **Verify each `[CRITICAL]` requirement with an answer-blind subagent** (recipe B in `references/subagents.md`): hand it only the requirement verbatim + the candidate's cached datasheet path — never which part you favor — and have it try to *refute* satisfaction, citing the page. **Honor its verdict — you may not overturn a `fails`/`insufficient_evidence` with your own reasoning** (fix the inputs and re-run, reselect, or escalate to the user); record the verdict + citation into `{project}_07_traceability.yaml`. A part that fails any [CRITICAL] requirement is **disqualified regardless of price, stock, or library support** — do not rationalize around it. And **every candidate the spec named must be evaluated here** (or an explicit, justified reason given for dropping it); never silently substitute a requirement away. *(This is the gate that the BQ24650 selection bypassed — it failed the spec's "[CRITICAL] SYS live regardless of battery" requirement, and the spec's power-path candidate was never put on the table.)*
- [ ] **Works** — electrically correct for the application (specs match requirements)
- [ ] **PCBway-sourceable** — in stock at a distributor PCBway sources from (LCSC/DigiKey/Mouser) with a captured distributor PN, **and** an assembly-friendly package per the rubric
- [ ] **KiCad ready** — has symbol and footprint in KiCad's built-in libraries (or user has sourced them)

A part cannot move from Stage 2 to Stage 3 unless all four gates pass.

**Fill out the candidates template** (`templates/02_component_candidates.md`).

**What to discuss with the user:**
- Package tradeoffs (SOT-23-5 is easier and cheaper to assemble; QFN is smaller but pitch matters)
- Cost vs performance tradeoffs
- Parts they've used before and trust
- Whether a cheaper/more-stocked equivalent would assemble more reliably at PCBway
- Alternative approaches (e.g., discrete vs integrated solution)

**Output:** Completed `{project_name}_02_candidates.md` with user selections marked.

**Gate:** User selects a part for each role before proceeding to Stage 3.

---

## Stage 3: BOM

**Input:** User's selections from Stage 2.

**What Claude does:**
1. Build the final BOM from selected parts using the template (`templates/03_bom.md`)
2. Assign reference designators (U1, R1, C1, etc.)
3. **For ICs**: use the `lib_id` and `footprint` from `check_kicad_library.py` (captured in Stage 2). These go directly into the BOM's KiCad columns — no manual lookup needed.
4. **For passives**: map the package to a KiCad footprint using `footprint_map.yaml` (e.g., `capacitors["0805"]` → `Capacitor_SMD:C_0805_2012Metric`)
5. Calculate quantities (including decoupling caps — one per IC plus bulk)
6. **Fill the distributor columns** — every line gets a Supplier + Supplier PN (LCSC PN preferred). This is the data PCBway needs to source the part.
7. Calculate estimated parts cost

### PCBway sourceability check (this stage's gate)

The BOM is what PCBway sources against, so verify it's buildable before the user signs off. Two layers:

**Layer 1 — deterministic rubric check** (offline, runs over the BOM markdown):
```bash
python check_pcbway.py {project_name}_03_bom.md --json
```
- **0 blocking (error) issues required.** Blocks mean a part can't be assembled/sourced as written (un-assemblable package like 01005, missing footprint, or an IC with no part number). Fix the BOM and re-run.
- **Review every caution.** Through-hole parts, BGA, 0201, obsolete/EOL/NRND, single-source, MSL — each is fine *if intentional*. Either swap the part or note why it's acceptable.

**Layer 2 — live distributor stock** (web search — PCBway has no parts API, so this can't be scripted):
- For each line, confirm the captured distributor PN is **actually in stock now** at LCSC/DigiKey/Mouser (`assembly.distributor_order`). Stock from Stage 2 can go stale.
- For any line still missing a distributor PN, find an in-stock one and add it.

Then emit the **sourcing sheet** the user hands to PCBway:
```bash
python check_pcbway.py {project_name}_03_bom.md --sourcing-sheet -o {project_name}_03_sourcing.md
```

### Requirements coverage check (this stage's other gate — catch selection errors *here*, not at Stage 7)

The moment the BOM exists, you have everything needed to check it against the spec — so do it now, at the cheapest point to fix. Author `{project_name}_07_traceability.yaml` (`templates/07_traceability.yaml`) mapping **every** Stage-1 requirement to the selected part(s) (`satisfied_by`) with cited evidence, then run the backstop:
```bash
python check_requirements.py {project_name}_01_specification.md {project_name}_07_traceability.yaml {project_name}_03_bom_flat.md
```
At this stage the check answers **"did we *select* parts capable of every requirement?"** — the evidence is datasheet capability (you can't cite topology yet; that comes at Stage 7). **0 errors required.** A `[CRITICAL]` requirement with no part to satisfy it (`untraced_requirement`) means the *selection is wrong* — **go back to Stage 2 and reselect before building anything downstream.** This is exactly the gate that would have caught choosing a non-power-path charger against a spec that demanded one. (You'll re-run the same matrix at Stage 7 with the realized-topology evidence filled in.)

**Output:** Completed `{project_name}_03_bom.md` + `{project_name}_03_sourcing.md` (PCBway sourcing sheet) + `{project_name}_07_traceability.yaml` (first pass).

**Gate:** User confirms BOM before proceeding to Stage 4. `check_pcbway.py` must show 0 blocking issues, every line must have a confirmed in-stock distributor PN, **and `check_requirements.py` must show 0 errors** (every requirement, especially `[CRITICAL]`, has a selected part with cited evidence).

---

## Stage 4: Implementation Reference

**Input:** Approved BOM from Stage 3.

**What Claude does:**
1. For each IC in the BOM, **first check the pinout database**:
   ```python
   from lookup_pinout import lookup_pinout, get_ic_pins_for_generator
   pinout = lookup_pinout("AP2112K-3.3")
   if pinout:
       # Use verified pinout — no hallucination risk
       pins = get_ic_pins_for_generator("AP2112K-3.3")
   ```
   If the part is in the database, use the verified pinout. If not, research via web search:
   - `"{part_number}" datasheet pinout`
   - `"{part_number}" application circuit`
   - `"{part_number}" reference design schematic`
2. Fill out the implementation reference template (`templates/04_implementation_reference.md`) with:
   - **Complete pinout table** — every pin, its type, and what it connects to in this design
   - **Application circuit** from the datasheet — ASCII art showing the recommended circuit with specific component values
   - **Critical design notes** — minimum cap values, enable pin handling, thermal concerns, pins that must not float
   - **Datasheet reference** — URL, relevant page/figure numbers
3. Build the **passive sizing table** — every passive with its value, purpose, and how the value was determined (datasheet reference, calculation, or standard practice)
4. Build the **net plan** — every named net, its type (power/signal), source, and loads

**This is the most important stage for correctness.** The implementation reference becomes the blueprint for schematic generation. Errors here propagate directly into the schematic.

**Independent pinout re-derivation (for any IC not in the pinout DB).** Hallucinated
pinouts are the top risk here, so verify each non-DB IC's pinout with an **answer-blind
subagent** (recipe C in `references/subagents.md`): hand it only the MPN + the cached
datasheet path — **not** the card's `pins:` block — and have it derive the pinout from
the datasheet alone. **Diff** its result against the `{MPN}.facts.yaml` card; any
disagreement on a pin number/name/type is a **stop** until resolved against the
datasheet and the card is corrected. Only then set **`pinout_verified: true`** on the
card — that flag is what lets its pinout flow into the Stage 6 layout `symbols:`
number/name/type. DB-seeded parts are already verified; this guards exactly the parts
where the risk is real and yields a card eligible for promotion into `pinout_db.json`.

**What to discuss with the user:**
- Do the pinout assignments look correct? (Especially for ICs the user has used before)
- Are the application circuits appropriate for this use case? (Some ICs have multiple configurations)
- Any concerns about specific passive values?
- Does the net plan make sense?

**Output:** Completed `{project_name}_04_implementation.md`.

**Gate:** User verifies pinouts and application circuits before proceeding to Stage 5.

---

## Stage 5: DC Analysis

**Input:** Approved implementation reference from Stage 4.

**What Claude does:**
1. Generate a design analysis YAML file from the implementation reference (use `templates/04b_design_analysis.yaml` as the schema reference)
2. The YAML captures every analyzable subcircuit:
   - **Power rails** with current budgets (sum all loads vs source capacity)
   - **LDO/regulator blocks** (dropout, thermal, cap sizing)
   - **Voltage dividers** (output vs target, tolerance)
   - **Feedback dividers** for adjustable regulators
   - **LED circuits** (forward current from Vsource, Vf, R)
   - **Pull-up networks** (I2C rise time, sink current, value sanity)
   - **Capacitor sizing** (value vs datasheet min, MLCC voltage derating)
   - **Custom/unanalyzable subcircuits** (flagged for manual review)
3. Run the DC analysis engine:

```python
from analyze_dc import load_design_from_string, analyze, format_result_text

design = load_design_from_string(yaml_text)
result = analyze(design)
print(format_result_text(result))
```

Or from CLI:
```bash
python analyze_dc.py design.yaml
python analyze_dc.py design.yaml --json
python analyze_dc.py design.yaml --strict   # warnings also fail
```

**The analyzer checks:**

| Subcircuit Pattern  | What it validates                                          |
|--------------------|------------------------------------------------------------|
| `current_budget`   | Total load per rail vs source capacity, margin warnings    |
| `ldo_regulator`    | Dropout headroom, overcurrent, Tj thermal calc, cap mins   |
| `voltage_divider`  | Vout vs target within tolerance, quiescent current, power  |
| `feedback_divider` | Vout = Vref*(1+Rtop/Rbot) vs target for adjustable regs   |
| `led_circuit`      | Forward current, max rating, visibility, resistor sizing   |
| `pullup_network`   | I2C rise time vs speed mode, sink current, value range     |
| `cap_sizing`       | Value vs datasheet min, MLCC voltage derating (2x rule)    |

**If errors are found:**
- Go back to Stage 4 and fix the implementation reference (change resistor values, cap sizes, etc.)
- Regenerate the design YAML
- Re-run the analyzer
- Iterate until the analyzer passes

**If only warnings remain:** Review each warning. Either fix it or document why it's acceptable.

**Output:** `{project_name}_04c_analysis.md` — DC analysis report.

**No user gate** — this is an automated check. If it passes, proceed directly to Stage 5b. If it fails, fix and re-run (user is consulted only if fixes require design changes they need to approve).

---

## Stage 5b: Netlist Declaration

**Input:** Approved implementation reference from Stage 4, passing DC analysis from Stage 5.

**What Claude does:**
1. Generate a pin-level netlist YAML file from the implementation reference (use `templates/05b_netlist.yaml` as the schema reference)
2. The YAML declares **every connection in the design as text**, with no geometry or coordinates:
   - **Component manifest** — every component and its pin list (from BOM + datasheet)
   - **Named nets** — each net lists every (ref, pin) pair that belongs to it, plus power symbols and labels
   - **No-connects** — every unconnected pin with a reason
3. **Every pin on every component** must appear in exactly one net or in `no_connects`. This completeness constraint catches forgotten connections before any wires are drawn.

**Example structure:**
```yaml
project: "My Board"
source: "my_board_04_implementation.md"

components:
  U1:
    part: "AP2112K-3.3"
    pins: ["1", "2", "3", "4", "5"]
  C1:
    part: "100nF"
    pins: ["1", "2"]

nets:
  "+3V3":
    type: power
    pins:
      - { ref: U1, pin: "5", function: "VOUT" }
      - { ref: C1, pin: "1", function: "output decoupling" }
    power_symbols: ["+3V3"]
  GND:
    type: power
    pins:
      - { ref: U1, pin: "2", function: "GND" }
      - { ref: C1, pin: "2", function: "decoupling" }
    power_symbols: ["GND"]

no_connects:
  - { ref: U1, pin: "4", reason: "NC per datasheet" }
```

**Why this stage exists:** It separates *what connects to what* (design intent) from *where things are drawn* (geometry). Stage 6 can focus purely on layout and routing. Stage 7 can verify the schematic matches this document exactly — a binary pass/fail per net.

**Self-check:** Before proceeding, verify the YAML is internally consistent:
- Count pins in `components` vs pins assigned in `nets` + `no_connects` — they must match
- Every net with `power_symbols` should have `type: power`
- No pin appears in more than one net

### Classify sensitive nets (enables the analog-noise checks)

If the design has any sensitive analog content (ADC inputs, sensor front-ends,
bridge/load-cell signals, references, RF or switching nets), tag those nets with
a `class:` in the netlist YAML. This is cheap and seeds both the front-end check
below and the Stage 8 layout check. See the schema notes in
`templates/05b_netlist.yaml` and the reasoning in `references/analog_layout.md`.
Key classes: `analog`, `analog_differential` (with `pair:` / `polarity:` /
`source_z:`), `analog_supply`, `reference`, `high_impedance`, `rf`, `switching`.

### Analog front-end completeness check (gate for sensitive designs)

Run the analog analyzer on the netlist **before generating the schematic** — a
missing input filter or decoupling cap is a schematic defect you cannot fix in
layout:

```bash
python analyze_analog.py {project_name}_05b_netlist.yaml
python analyze_analog.py {project_name}_05b_netlist.yaml --json
```

```python
from analyze_analog import analyze_netlist_from_string, format_result_text
result = analyze_netlist_from_string(yaml_text)
print(format_result_text(result))
```

It uses `analog_recipes.yaml` (per-part front-end knowledge, e.g. NAU7802) plus
the `class:` tags to check that every sensitive chain has what it needs:
- a **differential cap** (and ideally matched common-mode caps + series R) across
  each differential ADC/amp input — catches the classic "connector wired straight
  to the ADC pin with no anti-alias/EMI filter" defect;
- `Cdiff ≥ ~10× Ccm` so the filter can't unbalance the pair;
- **supply / reference / bandgap decoupling** per the part's recipe;
- a **ratiometric reference** tied to the bridge excitation, where supported.

**If it errors:** go back to **Stage 4** and add the missing front-end /
decoupling components to the implementation reference, then to the **Stage 3 BOM**
(so PCBway sources them), then re-declare the netlist and re-run. Add a recipe to
`analog_recipes.yaml` for any new sensitive part. **If only warnings remain:**
review each against `references/analog_layout.md` and either fix or document why
it's acceptable for this design.

**Output:** `{project_name}_05b_netlist.yaml`

**No user gate on connectivity** — but for sensitive designs the analog front-end
check above is a gate: fix errors before proceeding. The netlist YAML becomes the
source of truth for Stage 7 verification and the Stage 8 analog-noise check.

---

## Stage 6: Schematic Generation

**Primary path — the data-driven engine (`generate_from_data.py`).** Do not write a
per-project builder script. Instead author one **layout YAML** (geometry only) and let
the engine join it with the netlist and BOM to emit the `.kicad_sch`. Every fact has one
home: connectivity lives in the Stage 5b netlist, value/footprint in the Stage 3 BOM,
and placement + IC pin-side arrangement in the layout YAML. Nothing is retyped.

1. **Author `{project}_06_layout.yaml`** using `templates/06_layout.yaml` as the schema and
   **`references/layout_authoring.md` for the conventions + worked examples**. This is an
   *authoring* step — Claude's judgment, not a script. It contains only: `power_nets`,
   `placements` (ref → lib_id + x/y/rotation), and `symbols` (IC pin-side maps). Values and
   footprints are **not** here — they come from the BOM. Procedure:

   a. **Resolve each component.** lib_id + footprint from the Stage 3 BOM and
      `check_kicad_library.py --lookup`; packages via `footprint_map.yaml`. Passives and
      `Conn_01x0N` connectors auto-register from their lib_id — **skip them in `symbols:`**.
      Only true ICs / named-pin parts need a `symbols:` entry.

   b. **Author each IC's `symbols:` entry.** Each pin is `[number, name, type, side, index]`.
      The **number/name/type are intrinsic** — copy them from the IC's verified
      `{MPN}.facts.yaml` card (`pinout_verified: true`); never retype or invent them. The
      card was sourced from `check_kicad_library --lookup` / `lookup_pinout.py` / the
      datasheet and confirmed by the Stage 4 re-derivation.
      - **numbers** must equal that component's `pins` in the netlist *exactly* — the engine
        enforces this, so cross-check before running;
      - **name + type** must match the card's `pins:`;
      - **side + index** are your semantic-grouping decision (see `references/layout_authoring.md`):
        analog/signal inputs and primary power-in on the **left**, digital/outputs on the
        **right**, main supply on **top**, grounds on the **bottom**; keep differential pairs
        adjacent; keep enable/config pins near what they control.

   c. **Parts not in KiCad's built-in libraries** (e.g. NAU7802) — the split is *"does the
      symbol already exist in any registered library?"* (`check_kicad_library.py --lookup
      --project-dir {project_dir}`):
      - **`found: true`** (built-in or user lib, e.g. `Custom:NAU7802` from `kicad-import-lib`) →
        **embed it as-is.** Mark the `symbols:` entry `from_library: true` and the engine embeds
        the real symbol — its actual drawing *and* real pin geometry, exactly like KiCad placing
        the part. Author **no pins and no `side`/`index`** (that arrangement belongs to the symbol).
        Pass `--project-dir {project_dir}` / `--sym-lib` to `generate_from_data.py` so it resolves.
      - **`found: false`** (resolves nowhere) → author the symbol by hand: lib_id `Custom:<PART>`,
        footprint from the package / `footprint_map.yaml`, and pins from the datasheet with
        `side`/`index` grouping. This is the **only** case where `side`/`index` is your judgment.
        Better still, install a vendor symbol with **`kicad-import-lib`** and it becomes the first
        case.

      See the two worked examples (Case 1 / Case 2) in `references/layout_authoring.md`.

   d. **Author `placements:`** — a grid by functional block (ICs on a band, passives banded
      below, connectors at edges). Rotate multi-pin connectors so their stubs don't collide
      (the J3 balance-header → `rotation: 270` lesson in the reference).

2. **Pre-build gate — check the layout/BOM against the fact cards** (`check_cards.py`).
   This catches intrinsic-fact drift (a `symbols:` pin name/type or a BOM footprint that
   no longer matches the verified `{MPN}.facts.yaml` card) *before* the build, and enforces
   that every IC's card is `pinout_verified`:
   ```bash
   python check_cards.py {project}_06_layout.yaml {project}_03_bom_flat.md \
       --cards-dir {project}_datasheets --json
   ```
   **0 errors required.** On a mismatch, fix the *doc* to match the card (or, if the card
   itself is wrong, re-run the Stage 4 pinout re-derivation). This complements the engine's
   pin-*number* gate below by covering pin name/type + footprint, which the engine can't see.

3. **Generate:**
   ```bash
   python generate_from_data.py {project}_05b_netlist.yaml {project}_03_bom_flat.md \
       {project}_06_layout.yaml -o {project}.kicad_sch
   ```
   The engine runs pre-flight gates (every netlist component is placed and in the BOM;
   placement/BOM refs exist in the netlist; **each IC's symbol pin-set equals the netlist
   pin-set**; every pin is in exactly one net or a no-connect) and then, after building,
   self-verifies with the structural validator + netlist verifier + BOM cross-check.
   `missing_junction` is treated as a **blocking error** (a label-based schematic has no
   intentional T-joints, so a missing junction is an unintended wire collision — the
   balance-tap-to-GND short class). **The engine refuses to save a file with any error.**

4. **Fix loop (max 3 iterations).** Map each error to its cause and edit the *data*, not
   generated geometry:
   - `pin_name/type_mismatch` / `footprint_mismatch` / `pinout_unverified` (from
     `check_cards`) → reconcile the doc with the card, or re-run the Stage 4 re-derivation.
   - `no placement` / `no BOM line` / `not declared in netlist` → reconcile the three docs.
   - `symbol pin-set != netlist pin-set` → fix the pin numbers in `symbols:` (or the netlist).
   - `missing_junction` → a stub collided; nudge or **rotate** the offending part (rotating a
     multi-pin connector so pins face down gives each pin its own drop column).
   - other validator/verifier errors → adjust placement and regenerate.

   ```python
   from generate_from_data import generate
   res = generate(netlist_path, bom_path, layout_path, out_path, uuid_seed=None)
   # res.passed, res.errors, res.warnings  (use uuid_seed for reproducible output)
   ```

**BOM identity is baked into the symbols.** The engine writes each part's `MPN` (the
canonical key the PCBWay KiCad plugin reads), `Manufacturer`, `Package`, and `Description`
onto its symbol as hidden fields, so the plugin auto-populates the BOM at fab time — no
manual field entry. Every fitted line (passives included) needs a real MPN in the flat
BOM's Part Number column, or the Stage-9 [CRITICAL] `critical_schematic_mpn_present` gate
blocks it. The fields only reach the board after an **F8** "Update PCB from Schematic" sync.

**Output:** `{project}_06_layout.yaml` + the generated `{project}.kicad_sch`. Because the
file is regenerated from data, never hand-edit it — change the layout YAML and re-run.

**No user gate here** — proceed directly to Stage 7 verification.

### Fallback: the low-level builder API

The engine is built on the `KicadSchematic` builder library. Use it **directly only for
custom symbol shapes or one-off edge cases the layout schema can't express** — the
data-driven engine above is the path for virtually every board. The full builder
workflow (pre-flight checks, the 7-step label-based wiring procedure, and the API quick
reference) lives in **`references/builder_api.md`**. The same rule holds: the Stage 5b
netlist is the single source of truth for connectivity — never add or omit a connection
relative to it.

---

## Stage 7: Verification

**Input:** Generated `.kicad_sch` file from Stage 6.

**What Claude does:**

### Part A: Automated Validation

Run the validator:
```python
from validate_kicad_sch import validate, validate_file

# In-memory validation
result = validate(sch)

# Or file-based
result = validate_file("output.kicad_sch")
```

Or from CLI:
```bash
python validate_kicad_sch.py output.kicad_sch --json --netlist
```

The validator runs 12 checks:

| Check                  | Severity | What it catches                             |
|------------------------|----------|---------------------------------------------|
| `floating_pin`         | ERROR    | Pin not connected and not marked NC          |
| `dangling_wire`        | ERROR    | Wire endpoint going nowhere                  |
| `disconnected_label`   | ERROR    | Label not touching any wire or pin           |
| `duplicate_reference`  | ERROR    | Two components with same ref (R1, R1)        |
| `missing_lib_symbol`   | ERROR    | Component uses undefined lib_symbol (resolves nowhere) |
| `stale_lib_cache`      | WARNING  | Symbol missing from file cache, resolved from installed libs — re-save in KiCad |
| `single_pin_net`       | WARNING  | Net with only one connection (broken wire?)  |
| `missing_junction`     | WARNING  | T-join without junction marker               |
| `overlapping_components` | WARNING | Two components at same position             |
| `no_connect_conflict`  | WARNING  | NC marker where a wire exists                |
| `missing_power_source` | WARNING  | Power-input pin with no power source on net  |
| `similar_net_names`    | WARNING  | Case-only differences (VCC vs vcc)           |

**If errors are found:** Fix them and re-validate. Iterate until exit code 0.

### Part A2: Netlist Verification

Verify the schematic's connectivity matches the intended netlist from Stage 5b:

```python
from verify_netlist import verify, load_intended_netlist, load_intended_netlist_from_string

intended = load_intended_netlist("project_05b_netlist.yaml")
# or: intended = load_intended_netlist_from_string(yaml_text)
result = verify(intended, sch)
```

Or from CLI:
```bash
python verify_netlist.py project_05b_netlist.yaml output.kicad_sch --json
```

The verifier runs 3 checks:

| Check           | What it catches                                                |
|-----------------|----------------------------------------------------------------|
| `completeness`  | Pin not assigned to any net or no_connect; pin in multiple nets |
| `consistency`   | Netlist references a component/pin that doesn't exist in schematic |
| `connectivity`  | Pins declared on same net are on different nets in schematic    |

**This is the critical geometry-vs-intent check.** If the schematic validator (Part A) passes but the netlist verifier fails, the wiring is clean but doesn't match the design intent — fix the generation code and regenerate.

**If errors are found:** Fix and regenerate. Iterate until exit code 0.

### Part A3: BOM Cross-Check

Verify the schematic matches the BOM from Stage 3:

```python
from cross_check_bom import cross_check, load_bom_from_markdown
bom = load_bom_from_markdown(bom_md_text)
result = cross_check(bom, sch)
```

Or from CLI:
```bash
python cross_check_bom.py project_03_bom.md output.kicad_sch --json
```

Checks: every BOM entry exists in schematic, every schematic component exists in BOM, values and footprints match (with fuzzy value comparison: "100nF" == "0.1uF").

**If errors are found:** Fix and regenerate. Iterate until exit code 0.

### Part B: Claude Design Review

**First — Requirements traceability (the backward check against the verified spec).** Re-run the traceability matrix you first authored at Stage 3 (`{project}_07_traceability.yaml`), now **strengthening each item's evidence from datasheet *capability* to realized *topology*** — the part is actually wired as the spec demands, per the netlist, not just present on the BOM. Every **[CRITICAL]** item needs that topology-level cited evidence. A requirement with no home is a **finding that blocks delivery** (a dropped block, or a part that doesn't do what the spec demanded). Then re-run the deterministic backstop:
```bash
python check_requirements.py {project}_01_specification.md {project}_07_traceability.yaml {project}_03_bom_flat.md
```
It flags untraced requirements, hallucinated refs, [CRITICAL] items lacking evidence, and unaccounted ICs/connectors. **0 errors required.** (It verifies the matrix is complete and consistent; *you* still write honest evidence — it can't judge whether a citation is true. Use `EXTERNAL` in `satisfied_by` for off-board items like a pack BMS; leave it empty only to flag a requirement the design genuinely doesn't meet.) Do this before the structural checklist below.

Then walk through the design review checklist (`templates/05_design_review_checklist.md`).

Note: Many power/signal checks here overlap with Stage 5's DC analysis. The DC analyzer catches *value* errors quantitatively (wrong resistor → wrong voltage). This review catches *structural* and *semantic* errors the analyzer can't (wrong pin assignment, missing connection, polarity flip). Together they cover both classes.

**Run the structural review as an answer-blind subagent** (recipe D in
`references/subagents.md`) — it shouldn't inherit the main thread's belief that the
design is fine. Hand it the approved Stage-5b netlist YAML, the generated
`.kicad_sch`, the relevant cached datasheets, and the checklist in
`references/design_review.md`; it walks all five categories (Power Integrity, Signal
Integrity, Connectivity, Component Correctness, Footprint & BOM) and returns a findings
list `[{category, severity, location, issue, suggested_fix}]`. The main thread triages,
fixes real findings by editing the *data* and regenerating (per Stage 6), and records
results against `templates/05_design_review_checklist.md`. This complements — does not
replace — the deterministic validators in Parts A/A2/A3 and the `check_requirements`
pass above, which still run directly on the main thread.

**Output:** Completed `{project_name}_05_review.md` with checklist results.

### Part C: Fix and Re-verify

If any issues found in Part A or B:
1. Fix the issue in the generation code
2. Regenerate the schematic
3. Re-run validator
4. Re-check the failed checklist items
5. Repeat until clean

---

## Stage 8: PCB Layout Review

**Input:** User's PCB layout (`.kicad_pcb` file) after they have placed components and routed traces.

**Why this stage exists:** The schematic is electrically correct, but the PCB layout determines whether the board actually works. Bad component placement or trace routing can cause boost converter EMI, voltage droop, thermal failure, or decoupling that doesn't decouple. This review catches layout-level mistakes before fabrication.

**What Claude does:**

### Part A: Parse the PCB File

Read the `.kicad_pcb` file and extract:
1. **Component placements** — reference, position (x,y), layer, rotation. Flag any at (0,0) as unplaced.
2. **Track widths by net** — group traces by net name, report widths used. Flag power nets using signal-width traces.
3. **Copper zones** — list fills by net and layer. Flag missing ground pours.
4. **Board outline** — dimensions from Edge.Cuts layer.
5. **Via count and sizes** — verify layer transitions exist for multi-layer routing.

### Part B: Critical Distance Checks

Calculate center-to-center distances between components that have placement-sensitive relationships:

**For every switching regulator (boost/buck):**
- IC ↔ inductor: should be < 5mm, ideally < 3mm
- IC ↔ Schottky/synchronous FET: should be < 5mm
- Inductor ↔ diode: should be < 5mm (they share the switch node)
- IC ↔ input cap: should be < 5mm
- Diode/FET ↔ output cap: should be < 5mm
- The "hot loop" (IC SW pin → inductor → diode → output cap → IC GND) should be as small as possible

**For every IC:**
- IC ↔ each decoupling/bypass cap: should be < 5mm, ideally < 3mm
- Caps further than 10mm provide negligible decoupling — flag as BAD

**For feedback dividers:**
- Both resistors should be close together (< 5mm)
- Divider midpoint trace should be short and away from switch nodes

### Part C: Power Trace Width Check

Minimum trace widths for given currents (1oz copper, outer layer):

| Current | Min Width | Recommended |
|---------|-----------|-------------|
| 0.5A | 0.25mm | 0.5mm |
| 1.0A | 0.5mm | 0.75mm |
| 1.5A | 0.75mm | 1.0mm |
| 2.0A | 1.0mm | 1.5mm |
| 3.0A | 1.5mm | 2.0mm |

Check each power net's trace width against its maximum current from the DC analysis (Stage 5). Flag undersized traces.

### Part D: Thermal Check

- **Linear regulators / charger ICs:** Verify ground plane copper pour exists under the thermal pad or package. Check for thermal vias if the component has an exposed pad.
- **High-current paths:** Verify no bottlenecks (narrow trace segments in otherwise wide power paths).

### Part E: Layout-Specific Design Notes

Generate a text summary of **physical layout considerations specific to this design**. This is created during Stage 4 (implementation reference) but presented here for the user during layout review. Include:

- Switching regulator hot loop identification and routing guidance
- Component placement priorities (what must be close to what)
- Thermal dissipation requirements (ground pours, thermal vias)
- Sensitive signal routing (feedback dividers away from switch nodes)
- Current flow paths and where wide traces matter

Example format:
```
## PCB Layout Notes — {Project Name}

### Critical: MT3608 Boost Converter Hot Loop
The switching loop is: L1 pin 2 → U3 SW (pin 1) → D1 anode → D1 cathode → C5 (+) → C5 (-) → U3 GND.
This loop carries high-frequency switching current at 1.2MHz. It MUST be as small and tight as possible.
- Place L1, U3, D1, and C5 in a tight cluster with minimal trace length between them
- Route the SW node trace short and wide — it carries the full inductor current
- Keep the GND return from C5 back to U3 short
- Do NOT route sensitive signals (FB_NET) near the SW node

### TP4056 Thermal Management
The TP4056 dissipates up to 0.8W at peak charging. It needs:
- Solid ground plane copper pour on both sides under the SOIC-8 package
- Thermal vias (0.3mm, array of 4-6) connecting F.Cu ground pad to inner/back ground planes
- C1 (input) and C2 (battery) within 3mm of their respective pins
```

### Part F: Report

Present findings as a structured report with:
- PASS/WARN/FAIL per check category
- Specific distances and trace widths with recommendations
- An overall assessment of layout readiness

### Part G: Analog Noise / Signal-Integrity Check

For boards with sensitive analog content, run the layout backstop. This is the
**backstop, not the main event** — the decisive analog defects (missing filter
caps) were already gated at Stage 5b. This stage checks how the *routing* treats
the sensitive nets:

```bash
python analyze_pcb_si.py {project_name}.kicad_pcb --netlist {project_name}_05b_netlist.yaml
python analyze_pcb_si.py {project_name}.kicad_pcb --json
```

Pass `--netlist` so the check uses the authoritative `class:` tags; without it,
it falls back to net-name heuristics (`*SIG*`, `*_P`/`*_N`, etc.). It flags:

- **diff-pair asymmetry** — legs on different layers / unequal length / unequal
  via count (kills common-mode rejection);
- **reference-layer** — a sensitive net routed on a layer that references a
  *power* plane, not GND (return-path / split-crossing risk);
- **aggressor proximity** — a sensitive trace running close to / under an RF or
  switching part (e.g. a WiFi module);
- **long runs**, **via-in-pad** on solderable pads, and **return vias** (a
  sensitive layer-change via with no nearby GND via).

**Interpreting severity — read `references/analog_layout.md` first.** These are
mostly warnings/advisory by design. The guard-pour advisory is **source-impedance
aware**: for a low-impedance bridge (load cell), guarding is *low priority* — do
**not** sacrifice a tight form factor for guard copper; the solid GND plane +
diff-pair symmetry do the work. For a high-impedance node, guarding is
*recommended*. Spend effort in proportion to payoff.

**If critical issues found:** Explain the problem, why it matters electrically, and suggest a specific fix (move component X closer to Y, widen trace Z to 1mm, add ground pour on F.Cu).

**Output:** `{project_name}_08_layout_review.md`

**Gate:** User reviews and makes layout changes before ordering.

---

## Stage 9: PCBway Upload Package

**Input:** A design that passed Stage 7 (schematic verified) and Stage 8 (DRC-clean routed `.kicad_pcb`).

This stage produces the three artifacts PCBway turnkey needs — **BOM, gerbers+drill, centroid** — each *generated from the source-of-truth files*, never hand-edited. Hand-editing the upload spreadsheet is exactly how stale/wrong cells (an LCSC code sitting in the MPN column, a package that disagrees with the part) reached PCBway before. Everything lands in `{project}/PCBway_uploads/`. Run the four steps in order; each is a gate.

**1. Structural BOM gate (deterministic, offline).**
```
python check_pcbway.py {project}_03_bom.md --json      # 0 blocking required
```
Catches the offline-detectable defects: a distributor code (LCSC `C…`) in the **Mfg Part #** column, a description where an MPN belongs, the **package field disagreeing with the footprint**, a missing **Manufacturer**. (Connectors/THT and unrecognized footprints are cautions — confirm intended.)

**2. Answer-blind per-line verification (the live gate).** A *well-formed* distributor code can still resolve to the wrong part — only an independent live lookup catches it (this is how `C914291`=Zener and `C13564`=wrong-package-sibling slipped past every offline check).
```
python bom_verify.py {project}_03_bom.md --worklist     # → lines needing a live check
```
For each worklist line, spawn an **answer-blind subagent** (recipe E in `references/subagents.md`) given ONLY the claim `{manufacturer, MPN, value, package, distributor code}`; it web-verifies independently and returns a verdict. Collect the verdicts JSON, then:
```
python bom_verify.py {project}_03_bom.md verdicts.json --report -o PCBway_uploads/verification_report.md
```
**0 mismatches required** — a `mismatch` is a confirmed wrong/inconsistent part and blocks the upload. Coverage is non-passives + any structurally-flagged line (`--all` also verifies clean passives).

**3. Generate the PCBway BOM (pure transform).**
```
python generate_pcbway_bom.py {project}_03_bom.md --output-dir PCBway_uploads
```
`bom.md` → PCBway's 9-column upload form (real Manufacturer + Mfg Part # on **every** line, passives included; LCSC code in the Notes column; identical parts grouped). Fix data in `bom.md`, never in the xlsx.

**4. Generate the fab outputs (DRC-gated).**
```
python generate_fab_outputs.py {project}.kicad_pcb --output-dir PCBway_uploads
```
Refuses to emit fab files from a board that fails DRC; auto-detects the copper stack (2/4/N-layer); exports gerbers + Excellon drill + centroid CSV; zips the gerbers.

**Output:** `{project}/PCBway_uploads/` — `{project}_PCBway_BOM.xlsx`, `gerbers/` + `{project}_gerbers.zip`, `{project}_centroid.csv`, `verification_report.md`, `fab_drc_report.json`.

**Gate:** 0 structural blocks, **0 verification mismatches**, DRC clean. A mismatch sends you back to fix `bom.md` (or the board) and **regenerate** — the upload is always a fresh generation, never a manual spreadsheet edit.

---

## Stage 10: Bringup & Field Report — the board meets reality

**Input:** A delivered board (Stage 9 shipped) — or ANY board of ours arriving
at / returning from the bench.

This is the stage that makes the whole system *accumulate* (ROADMAP W2):
every bench session must deposit something permanent. Non-gating for delivery,
but **the promotion ritual itself is non-optional** once bench results exist.

**Part A — Author the bringup checklist at delivery time** (with the Stage 9
package, before the board ships): fill `templates/10_bringup.md` from the
verified artifacts — expected voltages from the Stage-5 DC analysis, rails
from the netlist, I²C addresses from the fact cards, pins from
`generate_pinmap.py`'s `board_pins.h`. Every line measurable, every expected
value cited from an artifact (never re-derived from memory); `[ASK]` anything
the artifacts can't answer and resolve it with the user.

**Part B — Field report after bench time** (~10 minutes, user + Claude):
fill `templates/10_field_report.md` — checklist results, anomalies with
symptom/root-cause/evidence, and the promotion candidates.

**Part C — Promotion ritual** (`references/promotion.md` is the authority):
- **Failures** are routed by class to the cheapest layer that would have
  caught them — `pinout` → fact card + `pinout_db.json`; `topology` →
  `generate_from_data` pre-flight gate or analyzer pattern **with a
  regression test**; `sourcing` → `check_pcbway` gate / `bom_verify` coverage;
  `layout` → `analyze_pcb_si` check or the W3 seed list; `process` → the
  relevant SKILL/reference doc as a failure signature. (Worked example: the
  battery_3s CH224K VDD→VBUS failure became pre-flight gate #5 + test.)
- **Successes** promote: board → ledger status `validated`; proven
  subcircuits → block-extraction candidates (W1); silicon-confirmed pinouts →
  `pinout_db.json`; stable boards → Tier-3 eval anchors.

**Part D — Update the ledger** (`validated_boards.yaml`) and verify it:
```bash
python check_ledger.py --strict     # every claimed deposit must actually exist
```
The checker proves each lesson's `encoded_in` (`path::needle`) still resolves —
a refactor that orphans a bench lesson fails here instead of silently
forgetting it. A lesson that lives only in prose is not a lesson.

**Output:** completed bringup checklist, `{project}_field_report_{date}.md`,
updated `validated_boards.yaml` (0 errors), and the routed deposits.

---

## Final Delivery

Once Stage 7 passes, deliver to the user:

1. **`{project_name}.kicad_sch`** — The schematic file, saved to project outputs
2. **`{project_name}_03_bom.md`** — Finalized BOM (from Stage 3)
3. **`{project_name}_03_sourcing.md`** — PCBway sourcing sheet (from Stage 3) — the table to submit to PCBway turnkey assembly
4. **`{project_name}_04c_analysis.md`** — DC analysis report (from Stage 5)
5. **`{project_name}_07_review.md`** — Design review results
6. **`{project_name}_08_layout_review.md`** — PCB layout review
7. **`{project_name}/PCBway_uploads/`** — the Stage 9 upload package (PCBway BOM xlsx, gerbers+drill zip, centroid CSV, `verification_report.md`) — generated from source, 0 verification mismatches
8. **`{project_name}_10_bringup.md`** — the Stage 10 bringup checklist (expected voltages/currents/addresses cited from the verified artifacts), ready for bench day
8. **Summary message** including:
   - What was built and key design decisions
   - PCBway sourcing status — any lines still needing an in-stock distributor PN, or flagged by the rubric
   - Things to verify in KiCad before layout
   - Any caveats or assumptions

**For MCU boards, also generate the firmware pinmap handoff:**
```bash
python generate_pinmap.py {project_name}.kicad_sch -o {project_name}_firmware --sketch
```
Emits `board_pins.h` (every MCU GPIO ↔ named net, byte-deterministic, with a
skipped-pins audit trail) and a `bringup.ino` skeleton (I²C scan on the board's
SDA/SCL pins, status-LED blink). This closes the hardware/firmware seam — the
firmware starts from the schematic's pin truth instead of a human re-typing it.
MCU is auto-detected; pass `--mcu {ref}` if the board has several.

All intermediate documents (spec, candidates, implementation reference) and the
`{project_name}_datasheets/` cache (canonical datasheets + `index.md` citation ledger)
remain in the outputs directory for traceability.

**Always include this notice:**
> This schematic was AI-generated. Verify all pin assignments against datasheets before PCB layout. Run KiCad's ERC for additional checks not covered by the automated validator.

---

## Important Notes

### Symbol Library Definitions
Every component needs a corresponding `lib_symbols` entry. The generation script handles common passives (R, C, L, LED, D) automatically. For ICs, define pin configuration based on datasheet research from Stage 4.

### KiCad Version Compatibility
Generated files target KiCad 7/8 format (version 20230121). They open in KiCad 7.0+ and 8.0+.

### What the User Still Needs to Do After Receiving the Schematic
- Run ERC (Electrical Rules Check) in KiCad
- PCB layout (place components, route traces)
- Verify critical connections against datasheets
- Add board-level features (mounting holes, ground planes, edge cuts)
- Generate Gerbers for fabrication

### Coordinate System Reminders
- Origin: top-left
- Y increases downward (inverted from math convention)
- All positions snap to 1.27mm grid
- Rotations: 0, 90, 180, 270 degrees
- `get_pin_position()` handles Y-axis inversion between symbol space and schematic space

### Common Pitfalls
- Forgetting to define a lib_symbol before placing a component
- Wrong pin numbering for an IC (always verify against datasheet)
- Missing `instances` or `sheet_instances` sections (required for KiCad to load)
- Power symbols need `in_bom no` and `on_board no`
- Not snapping coordinates to 1.27mm grid (causes invisible disconnections)
