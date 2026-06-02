# Triggering corpus — should NOT trigger `kicad-schematic-gen`

Out-of-scope prompts. None should activate the skill. `test_evals.py` checks
(deterministically) that each is clearly out of domain — it shares **no** domain term with
the SKILL.md `description:`. This guards against an over-broad description that would
false-trigger. Note the deliberate near-miss: an electronics *question* that is not a *build*
request.

One prompt per `- ` line:

- Write a Python script to parse a folder of CSV files and total a column.
- Refactor this React component to reduce re-renders.
- Explain how a MOSFET works as a switch.
- What's the capital of France?
- Fix the failing test in my authentication flow.
- Summarize the key points of this contract PDF.
- Help me write a SQL query to join two tables.
