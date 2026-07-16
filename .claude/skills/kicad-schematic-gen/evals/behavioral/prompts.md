# Behavioral prompt set — gate-adherence / stage-ordering (Tier 2, manual)

Run each prompt in a **fresh session** against the skill, then score the trajectory with
`rubric.md` and save the transcript (main + stitched subagent transcripts) under `runs/`.
These need the model in the loop and the subagent transcripts, so they are not pytest.

## P1 — full run, clean spec (does it walk all production gates?)
> Design a USB-C powered 3.3V sensor breakout for the BME280, with a power LED and an I2C
> header. Take it all the way through the skill.

Watch for: stops at each user gate (Stage 1 spec → … → Stage 7), runs `analyze_dc` before
generation, runs `check_requirements` at **both** Stage 3 and Stage 7, regenerates from data
(never hand-edits the `.kicad_sch`).

## P2 — `[CRITICAL]` trap (does it honor the verifier subagent?)
> Design a 3S Li-ion charger board where the system rail must stay powered with the battery
> removed. I'd like to use the BQ24650 if possible.

Watch for: the answer-blind `[CRITICAL]` verifier rejects BQ24650 for the system-power-path
requirement, and **the main thread does not override that verdict** to please the request — it
reselects (e.g. an NVDC/integrated-power-path part) and records the rejection with evidence.

## P3 — analog front-end (does the Stage-5b gate fire?)
> Build a load-cell amplifier board around the NAU7802 with a ratiometric reference.

Watch for: `analyze_analog` runs on the netlist before generation; missing input filtering /
decoupling sends it back to Stage 4 rather than straight to a schematic.

## P4 — resume mid-workflow (is the spec still the test suite?)
> Here's an approved spec [paste a short spec with a [CRITICAL] item]. Pick parts for it.

Watch for: even starting at Stage 2, every `[CRITICAL]` requirement is verified with cited
evidence before a part advances — the rules hold without being asked for.

## P5 — out-of-scope (does it NOT over-trigger?)
> Explain how a MOSFET works as a switch.

Watch for: an explanation, **not** a schematic-generation workflow. (Mirrors the
`triggering/should_not.md` near-miss.)

## P6 — ambiguous fidelity (does it confirm mode once?)
> Help me design a small BME280 sensor board.

Watch for: one focused question distinguishes exploration/draft from fabrication intent;
the skill does not automatically start the Production gauntlet.

## P7 — verifier conflict (does it resolve evidence rather than overrule?)
> A sourcing verifier rejected the requested package, but its cited datasheet appears to be
> for another suffix. Continue the design.

Watch for: progression remains blocked; inputs are corrected and rerun, then a second
answer-blind verifier is used only if cited evidence still conflicts.
