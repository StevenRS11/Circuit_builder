# Subagents and the datasheet cache

This is the detailed playbook for the subagent doctrine summarized in SKILL.md
("Subagents and the datasheet cache"). Read it before launching subagents at
Stage 2, 4, or 7.

## Why subagents here

The design hierarchy's core rule is *"re-check against verified documents at every
gate, not the current step's own reasoning."* The main thread **can't** do that
honestly — it has already reasoned itself into its choices, so it has sunk-cost
bias toward them. A subagent with **isolated context literally cannot see that
reasoning**, so its verdict is independent. Isolation is the feature, not a
limitation to work around.

That gives a sharp division of labor:

- **Use subagents for the `research` and the *non-scriptable* `verify` legs** —
  where fresh, answer-blind context is an asset.
- **Keep the `author` legs (Stages 5b/6) and every user gate on the main thread** —
  authoring needs the full accumulated design intent, and **subagents cannot talk
  to the user**, so anything that pauses for sign-off is inherently main-thread.
- **Never wrap the deterministic scripts in a subagent.** `validate_kicad_sch`,
  `verify_netlist`, `cross_check_bom`, `check_requirements`, `analyze_dc`,
  `analyze_analog`, `check_pcbway` are pure functions — call them directly. A
  subagent around them only adds latency and a layer that can hallucinate about
  their output.

**The context you hand a verifier is the whole design.** Because it starts empty,
you must hand it *explicit, frozen* inputs: the approved prior-stage doc + the
canonical datasheet, and nothing about the answer you're hoping for. If a check
can't be expressed that way, that's a smell that it depends on reasoning that
should have been written down.

## The datasheet cache

Research stages fetch datasheets; **save the canonical PDF once** so every
downstream verifier reads the *same* artifact the selection was based on (no
re-fetch drift, and citations like "datasheet p.7" resolve to a concrete file).

- **Location:** `{outputs}/{project_name}_datasheets/`
- **Filename:** `{MPN}.pdf` (exact manufacturer part number; sanitize `/` → `_`).
- **Index:** `{project_name}_datasheets/index.md` — a table with columns
  `MPN | Role | File | Source URL | Rev/Date | Retrieved`. This is the citation
  ledger; `check_requirements` evidence and Stage-2/4/7 citations point into it.
- **Save it** when a part is first evaluated against a `[CRITICAL]` requirement
  (Stage 2), and definitely for **every selected part** by Stage 3. Download with
  the shell, e.g. PowerShell `Invoke-WebRequest -Uri <url> -OutFile <path>` or
  Bash `curl -L <url> -o <path>`.
- **Pinout DB interplay:** if a part is in `pinouts/pinout_db.json`
  (`lookup_pinout.py`), that verified DB **is** the canonical pinout source —
  no datasheet fetch needed for pin data. Still cache the datasheet if a
  `[CRITICAL]` requirement needs evidence the DB doesn't carry (e.g. dropout,
  thermal, power-path behavior).
- Subagents read the cached PDF directly with the Read tool (it pages through
  PDFs). Pass the **file path**, not the URL — that's what makes the check
  reproducible.

## Per-part fact cards

Alongside the datasheet PDF, the research subagent saves a **structured fact card** so
the part's *intrinsic* facts become a durable, diffable artifact instead of prose the
main thread retypes. The verify subagents then check against the card — that's the
point of having it.

- **Location:** `{outputs}/{project_name}_datasheets/{MPN}.facts.yaml` (next to the PDF).
- **Scope — intrinsic facts only.** A card holds what is true of the *part on any
  board*: sourcing, package, `lib_id`, `footprint`, and the **pinout (number / name /
  type)**. It must **not** contain *relational* per-design facts — connectivity,
  placement, `side`/`index`, or passive sizing. Those are board-specific **judgment**
  that stays Claude-authored in the netlist/layout (the design hierarchy's "never script
  the judgment" line). The split mirrors the engine's inputs: card → intrinsic columns
  of the BOM + the `symbols:` pin number/name/type; netlist/layout → everything
  relational.
- **Schema:**
  ```yaml
  # {MPN}.facts.yaml — intrinsic per-part facts. No per-design connectivity.
  mpn: "NAU7802SGI"
  role: "load-cell ADC"
  package: "QFN-16"
  datasheet: "NAU7802SGI.pdf"        # relative to the datasheets folder
  sourcing: { distributor: "LCSC", distributor_pn: "C2557845", stock: 4200, price: "$1.10" }
  kicad:   { lib_id: "Custom:NAU7802", footprint: "Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm", found: true }
  pins:                              # the pinout — number/name/type ONLY (intrinsic)
    - { number: "1", name: "VDDA", type: "power_in",  page: 12 }
    - { number: "2", name: "VINP", type: "input",     page: 12 }
    # ...
  pinout_verified: false             # flip to true once recipe C's re-derivation agrees
  ```
- **Pinout-DB interplay & flywheel:** the `pins:` block is the same shape as a
  `pinouts/pinout_db.json` entry. If the part is already in the DB, seed the card from
  the DB (`pinout_verified: true`). After a card's pinout passes recipe C, it's a
  **candidate for promotion into `pinout_db.json`** — that's how the 10-part DB grows
  and how future runs skip the datasheet fetch entirely.

## Subagent recipes

Each recipe lists the frozen context to hand the subagent, what it returns, and
how the main thread uses the result. Spawn research/verify subagents in parallel
when independent (one per candidate / per requirement). Use a `general-purpose`
subagent for research (it needs web + shell to fetch and cache); a read-only agent
is fine for verifiers that only Read the cached datasheet + docs.

### A. Stage 2 — per-candidate research fan-out

- **One subagent per functional role.** Hand it: the role, the **spec requirements
  that apply to that role** (verbatim, including any `[CRITICAL]` flags), the
  PCBway selection rubric, the `parts.preferred` / connector / regulator lists from
  `preferences.yaml`, and `assembly.distributor_order`.
- **It does:** identify 2-3 candidates, run `check_kicad_library.py --lookup` for
  each, confirm distributor stock via web search, **cache each serious candidate's
  datasheet** to the folder + index, **write each one's `{MPN}.facts.yaml` fact card**
  (sourcing + `lib_id`/`footprint` + extracted pinout, `pinout_verified: false`), and
  score each against the rubric.
- **Returns (structured):** per candidate — `{MPN, package, price, distributor,
  distributor_pn, stock, kicad: {lib_id, footprint, found}, datasheet_path,
  card_path, rubric_pass, pros, cons}`, plus a recommended winner with reasoning.
- **Main thread:** collates the rows into `templates/02_component_candidates.md`
  and presents them at the user gate. The 20-30 searches per role never enter the
  main context — only the distilled rows do. The cards are the authoritative source
  for the intrinsic fields carried into the BOM/layout (see consumption contract below).

### B. Stage 2 — `[CRITICAL]` requirement verifier (answer-blind)

This is the BQ24650-class catch. **One subagent per `[CRITICAL]` requirement, per
candidate seriously in contention.**

- **Hand it ONLY:** the requirement text **verbatim**, the candidate MPN, and the
  **cached datasheet path**. Do **not** tell it which part you favor or what verdict
  you expect — that would let it anchor.
- **Prompt skeleton:** *"Here is requirement R{n}: '{verbatim}'. Here is the
  datasheet for {MPN} at {path}. Does this part satisfy this requirement? Try to
  **refute** it. Cite the exact page/figure/table and quote the line. If the
  datasheet does not clearly support it, answer `fails` or `insufficient_evidence`
  — default to that when uncertain."*
- **Returns:** `{requirement_id, verdict: satisfied|fails|insufficient_evidence,
  citation, quote, reasoning}`.
- **Main thread:** any `fails` on a `[CRITICAL]` requirement **disqualifies the
  part regardless of price/stock/package** — go back and reselect. The returned
  citation feeds the Stage-3/7 traceability YAML evidence.

### C. Stage 4 — independent pinout re-derivation (verifies the card)

- **Hand it ONLY:** the MPN and the cached datasheet path (or note it's in the
  pinout DB). **Do not** give it the card's `pins:` block or any pinout the main
  thread drafted — it must derive the table from the datasheet alone, or it will just
  echo your guess.
- **Returns:** the complete pinout table `[{number, name, type, page}]` derived
  fresh from the datasheet.
- **Main thread:** **diff the subagent's table against the card's `pins:` block**
  (`{MPN}.facts.yaml`). Any disagreement on a pin number/name/type is a **stop** —
  resolve against the datasheet, correct the card, and re-run. Only when they agree do
  you set **`pinout_verified: true`** on the card; that flag is the gate that lets the
  card's pinout flow into the layout `symbols:` number/name/type. (For DB-seeded parts
  the DB is authoritative; this guards the non-DB parts where hallucination risk is
  real, and produces a card eligible for promotion into `pinout_db.json`.)

### D. Stage 7 — structural design review (answer-blind)

- **Hand it:** the approved Stage-5b netlist YAML, the generated `.kicad_sch`, the
  relevant cached datasheets, **the `{MPN}.facts.yaml` cards** (the verified intrinsic
  facts), and the checklist in `references/design_review.md`.
- **It does:** walk all five checklist categories (Power/Signal Integrity,
  Connectivity, Component Correctness, Footprint & BOM) against the actual netlist
  + schematic, with no knowledge of the main thread's belief that the design is fine.
  It uses the cards as ground truth for the **Component Correctness** and **Footprint &
  BOM** rows — every IC pin used in the schematic must match the card's `pins:`, and
  each footprint must match the card's `kicad.footprint`.
- **Returns:** findings list `[{category, severity, location (ref/pin/net),
  issue, suggested_fix}]`.
- **Main thread:** unlike the binary verdicts of B/C, this returns a findings *list*
  that you triage — but **triage means investigate each finding, not dismiss it.**
  Discard one only with a concrete, recorded reason (not "the design looks fine");
  fix the real ones by editing the *data* and regenerating (per Stage 6); record the
  disposition of every finding in `{project}_05_review.md`. This complements — does
  not replace — the deterministic Stage-7 validators and the `check_requirements`
  traceability pass, both of which still run directly on the main thread.

## How the main thread consumes subagent results

A subagent only adds value if the main thread **honors what it returns**. The whole
point is that the subagent saw the question without the main thread's bias — so the
main thread must not then re-introduce that bias when reading the answer.

### Respecting the verify results (recipes B, C, D)

- **A verdict is authoritative within its scope. Do not overturn a `fails` /
  `insufficient_evidence` / disagreement with your own reasoning.** That reasoning is
  exactly the biased input the subagent was spawned to bypass; "the checker was too
  strict" is not a finding. A failing check has only **three** legitimate responses:
  1. **The inputs were wrong/incomplete** (wrong or truncated datasheet cached, missing
     page) → fix the *frozen inputs* and **re-run the same check**.
  2. **The selection/design is wrong** → reselect (Stage 2) or fix the data and
     regenerate (Stage 6/7). A `[CRITICAL]` `fails` is a hard disqualification.
  3. **Genuine ambiguity** → surface the verdict **and its citation** to the user and
     let them decide. Never a silent "proceed anyway."
- **Persist every verdict + citation into the durable artifact**, not just the chat:
  `[CRITICAL]` verdicts → the `evidence` field of `{project}_07_traceability.yaml`;
  pinout diffs → a note in the implementation reference; review findings →
  `{project}_05_review.md`. A verdict that lives only in working memory is lost at the
  next context compaction and can't be audited.
- **Don't over-respect, either.** A verifier is scoped to its inputs — anything it
  volunteers outside its remit is advisory, not binding. And a `satisfied` verdict is
  **not** a license to skip the deterministic scripts; `validate_kicad_sch`,
  `verify_netlist`, `cross_check_bom`, and `check_requirements` still run regardless.

### Using the research results (recipe A)

- **The returned rows *are* the candidate set — synthesize from them; don't re-run the
  research differently or backfill from memory.** You still own the final pick and the
  user gate, and you may disagree with a subagent's recommended winner — but base that
  on the **returned data**, not a recollection. If you need data the rows don't carry,
  **re-dispatch** the subagent; don't invent it.
- **The `{MPN}.facts.yaml` card is the authoritative source for every intrinsic field.**
  Carry `MPN`, `distributor_pn`, `lib_id`, `footprint`, and the pinout (number/name/
  type) **from the card** into the candidates doc → BOM → layout `symbols:` →
  traceability — **copy, never paraphrase or retype from memory** (that's the
  transcription drift the card exists to kill). These are the exact fields the
  deterministic scripts (`check_pcbway`, `cross_check_bom`, `generate_from_data`) and
  the verifier subagents consume.
- **Verify the authored docs back against the cards — run `check_cards.py`.** Before the
  Stage 6 build, the deterministic verifier joins the cards against the layout YAML + flat
  BOM (on `lib_id`) and fails on any drift: BOM `footprint` == `card.kicad.footprint`,
  each IC's `symbols:` pin `number`/`name`/`type` == `card.pins`, and the card's
  `pinout_verified` must be true (`pinout_unverified` is an error). It also warns on an
  IC `symbols:` entry with **no** card (`ic_without_card`; error under `--strict`).
  ```bash
  python check_cards.py {project}_06_layout.yaml {project}_03_bom_flat.md \
      --cards-dir {project}_datasheets --json
  ```
  A mismatch is drift — fix the *doc* to match the card (or, if the card is wrong, re-run
  recipe C). This is a **pure verifier, no judgment** — fully in-lane with the hierarchy.
  It complements the engine's `symbol pin-set == netlist pin-set` gate (which backstops
  pin *numbers*) by adding the name/type/footprint coverage the engine can't see.
- **Validate completeness on return, and don't proceed with a hole.** A subagent may
  return `null` (the user skipped it) or partial rows. Filter nulls; for any role
  missing a required field (no `lib_id`, no in-stock `distributor_pn`), **re-dispatch
  or do that one role's research on the main thread** — never advance a role with a gap.
- **The cached `datasheet_path` is the handoff to the verify subagents.** Pass that
  exact path (not a URL, not a re-fetch) to the recipe-B `[CRITICAL]` verifier and the
  recipe-C pinout re-derivation, so research and verification share one frozen artifact.

## Note for the eval suite

Fanning work into subagents **distributes the trajectory** across multiple
transcripts. The behavioral "gate adherence" / "stage-ordering" evals therefore
need to capture and stitch subagent transcripts, not just the main thread. Decide
the subagent topology and the eval trajectory-capture together.
