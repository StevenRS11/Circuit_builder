"""Eval suite — deterministic tiers (run in pytest, no model in the loop).

Tier 0  — full-pipeline regression: each corpus/synthetic/* case regenerates
          byte-deterministically (uuid_seed=0) and passes every applicable grader.
Tier 0b — [CRITICAL] negative regression: the frozen mis-selection cases under
          regression/* MUST fail their gate (proves the gate bites).
Tier 1  — triggering corpus: structural on-topic/off-topic checks + a drift guard
          that the SKILL.md description still advertises the core capabilities.

The model-in-the-loop tiers (gate adherence, actual activation) are a documented
manual protocol — see README.md — not part of this automated run.
"""
import os
import sys

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.normpath(os.path.join(_HERE, "..", "scripts"))
_GRADERS = os.path.join(_HERE, "graders")
for _p in (_SCRIPTS, _GRADERS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from run_all_verifiers import grade_case
from check_requirements import (
    load_spec_requirements, load_traceability, check_requirements,
)
from cross_check_bom import load_bom_from_markdown
from check_pcbway import load_bom_for_pcbway, check_bom_mpn_ready

_CORPUS_SYNTH = os.path.join(_HERE, "corpus", "synthetic")
_REGRESSION = os.path.join(_HERE, "regression")
_TRIGGERING = os.path.join(_HERE, "triggering")
_SKILL_MD = os.path.normpath(os.path.join(_HERE, "..", "SKILL.md"))


# ─── helpers ──────────────────────────────────────────────────────

def _subdirs(root, marker):
    if not os.path.isdir(root):
        return []
    return [os.path.join(root, d) for d in sorted(os.listdir(root))
            if os.path.isfile(os.path.join(root, d, marker))]


def _load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _prompts(md_path):
    """Pull one prompt per '- ' bullet line from a triggering corpus file."""
    out = []
    for line in open(md_path, encoding="utf-8").read().splitlines():
        s = line.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
    return out


def _skill_description():
    """Return the SKILL.md frontmatter `description:` (lowercased)."""
    text = open(_SKILL_MD, encoding="utf-8").read()
    parts = text.split("---")
    fm = yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
    return str(fm.get("description", "")).lower()


# Domain terms used to classify triggering prompts. Curated to be safe substrings
# (no bare "led"/"adc"/"board" that would false-match unrelated words).
DOMAIN_TERMS = [
    "schematic", "kicad", "breakout", "regulator", "charger", "sensor", "footprint",
    "power supply", "usb-c", "load-cell", "accelerometer", "ldo", "converter",
    "circuit", "bme280", "nau7802", "ch224k", "tp4056",
]
# Capabilities the description must keep advertising (drift guard).
CORE_TERMS = ["schematic", "kicad", "breakout", "power supply", "sensor", "charger"]


def _domain_hits(text):
    t = text.lower()
    return [k for k in DOMAIN_TERMS if k in t]


# ─── Tier 0 — full-pipeline regression ────────────────────────────

_SYNTH_CASES = _subdirs(_CORPUS_SYNTH, "case.yaml")


@pytest.mark.parametrize("case_dir", _SYNTH_CASES,
                         ids=[os.path.basename(d) for d in _SYNTH_CASES])
def test_corpus_case_passes_all_graders(case_dir):
    manifest = _load_yaml(os.path.join(case_dir, "case.yaml"))
    if manifest.get("expect", {}).get("passed", True) is not True:
        pytest.skip("case manifest does not expect a clean pass")
    report = grade_case(case_dir)
    failed = {n: g for n, g in report["graders"].items() if not g["passed"]}
    assert report["passed"], f"graders failed: {failed}"


@pytest.mark.parametrize("case_dir", _SYNTH_CASES,
                         ids=[os.path.basename(d) for d in _SYNTH_CASES])
def test_corpus_case_golden_byte_stable(case_dir):
    """The uuid_seed=0 regeneration must byte-match the committed golden."""
    report = grade_case(case_dir)
    gen = report["graders"].get("generate", {})
    assert gen.get("golden_match") is True, (
        f"golden drift in {os.path.basename(case_dir)} — "
        f"rerun: python evals/graders/run_all_verifiers.py {case_dir} --update-golden "
        f"if the change is intended"
    )


def test_at_least_one_synthetic_case():
    assert _SYNTH_CASES, "no synthetic corpus cases found"


# ─── Tier 0b — [CRITICAL] negative regression ─────────────────────

_NEG_CASES = _subdirs(_REGRESSION, "expect.yaml")


@pytest.mark.parametrize("case_dir", _NEG_CASES,
                         ids=[os.path.basename(d) for d in _NEG_CASES])
def test_negative_case_gate_bites(case_dir):
    exp = _load_yaml(os.path.join(case_dir, "expect.yaml"))
    assert exp.get("kind") == "negative", "non-negative case under regression/"
    grader = exp.get("grader")

    if grader == "check_requirements":
        spec = load_spec_requirements(os.path.join(case_dir, "spec.md"))
        trace = load_traceability(os.path.join(case_dir, "traceability.yaml"))
        bom = load_bom_from_markdown(
            open(os.path.join(case_dir, "bom_flat.md"), encoding="utf-8").read())
        result = check_requirements(spec, trace, bom)
    elif grader == "check_pcbway":
        # The schematic-MPN gate, exercised on the pre-generation flat BOM (a bad
        # MPN in the BOM is what bakes into an unresolvable symbol MPN).
        parts = load_bom_for_pcbway(
            open(os.path.join(case_dir, "bom_flat.md"), encoding="utf-8").read())
        result = check_bom_mpn_ready(parts)
    else:
        pytest.fail(f"unwired negative grader: {grader!r}")

    want = exp.get("expect", {})
    assert result.passed is want.get("passed", False), \
        f"expected passed={want.get('passed')}, got {result.passed}"
    must = want.get("must_include_check")
    if must:
        checks = {i.check_name for i in result.issues}
        assert must in checks, f"expected a '{must}' issue; got {sorted(checks)}"


def test_at_least_one_negative_case():
    assert _NEG_CASES, "no negative regression cases found"


# ─── Tier 1 — triggering corpus (structural) ──────────────────────

def test_description_keeps_core_capabilities():
    """Drift guard: trimming SKILL.md must not drop the advertised core triggers."""
    desc = _skill_description()
    assert desc, "SKILL.md frontmatter has no description"
    missing = [t for t in CORE_TERMS if t not in desc]
    assert not missing, f"SKILL.md description no longer mentions: {missing}"


def test_should_trigger_prompts_are_on_topic():
    prompts = _prompts(os.path.join(_TRIGGERING, "should_trigger.md"))
    assert len(prompts) >= 3, "should_trigger corpus too small"
    off = [p for p in prompts if not _domain_hits(p)]
    assert not off, f"should-trigger prompts with no domain term: {off}"


def test_should_not_prompts_are_off_topic():
    prompts = _prompts(os.path.join(_TRIGGERING, "should_not.md"))
    assert len(prompts) >= 3, "should_not corpus too small"
    bleed = [(p, _domain_hits(p)) for p in prompts if _domain_hits(p)]
    assert not bleed, f"should-not prompts that bleed domain terms: {bleed}"
