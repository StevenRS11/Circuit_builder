# Behavioral scoring rubric (Tier 2, manual)

Score each `prompts.md` run by reading the trajectory (main thread + the subagent transcripts
it spawned — they must be stitched in, since the trajectory is distributed). Mark each item
**pass / fail / n-a**, note the evidence (which message / tool call), and save alongside the
transcript in `runs/`.

## Triggering
- [ ] Activated the skill on the in-scope prompt; did **not** activate on the out-of-scope one (P5).

## Gate adherence
- [ ] Presented a document and **paused for user sign-off** at each applicable gate
      (Stage 1 spec, 2 candidates, 3 BOM, 4 impl ref, 7 verification) — did not auto-advance.
- [ ] Did not skip a stage or jump straight to a `.kicad_sch`.

## Stage ordering / script invocation (deterministically checkable from the tool-call log)
- [ ] `analyze_dc` ran **before** schematic generation.
- [ ] `check_requirements` ran at **Stage 3** and again at **Stage 7**.
- [ ] `analyze_analog` ran on the netlist before generation (analog designs, P3).
- [ ] `check_cards` ran before the build; the schematic came from `generate_from_data`, not a
      hand-edited file.

## Subagent doctrine & verdict honoring (the core of P2)
- [ ] Research / `[CRITICAL]` verification / pinout / review ran as **subagents** with frozen
      inputs (datasheet paths), not in the biased main thread.
- [ ] A failure blocked progression. The main thread checked frozen input identity,
      corrected and reran incomplete inputs, and used a second answer-blind verifier when
      cited evidence genuinely conflicted; unresolved conflicts were surfaced to the user.
- [ ] Verdicts + citations were **persisted** into the durable artifacts (traceability YAML /
      review doc), not left only in chat.

## Spec-is-the-test-suite
- [ ] Every `[CRITICAL]` requirement was checked with cited evidence, even on a mid-workflow
      resume (P4) — the rule held without being prompted for.

## Scoring
A run **passes** Tier 2 if every applicable box is checked. Any unchecked box is a finding:
record it, and if it traces to skill prose, that prose is the fix (the eval's whole point —
catching a SKILL.md change that silently broke a behavior).
