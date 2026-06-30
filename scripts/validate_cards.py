# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Heuristic spoiler-pattern check on build/cards.jsonl.

Catches the patterns we keep seeing in multi-cloze cards:

  1. NUMERIC_OVERLAP — same number appears in two different cN bodies
                       (e.g. c1=`70 mg/dL`, c2=`55 to <70 mg/dL`)
  2. NUMERIC_BOUNDARY — c1 has `≥X` / `>X` / `<X` while c2 contains a
                       range or value with the same X
  3. LITERAL_REPETITION — a cloze body string appears verbatim in the
                       surrounding text outside any cloze
  4. GRAMMAR_TELL — visible "a {{c1::vowel-word}}" or "an {{c1::consonant-word}}"

Usage:
    uv run scripts/validate_cards.py            # report-only, exit 0 unless --strict
    uv run scripts/validate_cards.py --strict   # exit 1 if any cards flagged

Pairs with `validate_cards_deep.py` for the LLM-eval audit (paid).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CARDS = REPO / "build" / "cards.jsonl"

CLOZE_RE = re.compile(r"\{\{c(\d+)::([^}]+?)(?:::[^}]+?)?\}\}")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")
BOUNDARY_RE = re.compile(r"[≥>]\s*(\d+(?:\.\d+)?)|[≤<]\s*(\d+(?:\.\d+)?)")
VOWEL_RE = re.compile(r"\b(a|an)\s+\{\{c(\d+)::([^}]+?)(?:::[^}]+?)?\}\}", re.IGNORECASE)


def strip_clozes(text: str) -> str:
    """Return card text with all {{cN::body}} replaced by an empty marker so
    we can scan only the surrounding/visible context."""
    return CLOZE_RE.sub("[...]", text)


def detect_numeric_overlap(text: str, groups: dict[str, list[str]]) -> list[str]:
    """Same numeric value across two distinct cN bodies."""
    issues = []
    nums_by_cn = {}
    for cn, bodies in groups.items():
        s = set()
        for b in bodies:
            for n in NUM_RE.findall(b):
                s.add(n)
        nums_by_cn[cn] = s
    cns = list(groups.keys())
    for i, a in enumerate(cns):
        for b in cns[i + 1:]:
            overlap = nums_by_cn[a] & nums_by_cn[b]
            if overlap:
                issues.append(f"NUMERIC_OVERLAP c{a}↔c{b}: shared {sorted(overlap)}")
    return issues


def detect_numeric_boundary(groups: dict[str, list[str]]) -> list[str]:
    """c1 = `≥X` or `>X` etc., c2 mentions same X in a range."""
    issues = []
    boundaries_by_cn = {}
    nums_by_cn = {}
    for cn, bodies in groups.items():
        b_set = set()
        n_set = set()
        for body in bodies:
            for m in BOUNDARY_RE.finditer(body):
                v = m.group(1) or m.group(2)
                if v:
                    b_set.add(v)
            for n in NUM_RE.findall(body):
                n_set.add(n)
        boundaries_by_cn[cn] = b_set
        nums_by_cn[cn] = n_set
    cns = list(groups.keys())
    for i, a in enumerate(cns):
        for b in cns[i + 1:]:
            shared_via_boundary = boundaries_by_cn[a] & nums_by_cn[b]
            if shared_via_boundary:
                issues.append(f"NUMERIC_BOUNDARY c{a}↔c{b}: ≥/<{sorted(shared_via_boundary)}")
            shared_other = boundaries_by_cn[b] & nums_by_cn[a]
            if shared_other:
                issues.append(f"NUMERIC_BOUNDARY c{b}↔c{a}: ≥/<{sorted(shared_other)}")
    return issues


def detect_literal_repetition(text: str, groups: dict[str, list[str]]) -> list[str]:
    """A cloze body's content appears verbatim in the visible surrounding text."""
    visible = strip_clozes(text).lower()
    issues = []
    for cn, bodies in groups.items():
        for body in bodies:
            body_clean = body.strip().lower()
            # Skip very short bodies — too noisy ("of", "and", numbers handled separately)
            if len(body_clean) < 6:
                continue
            if body_clean in visible:
                issues.append(f"LITERAL_REPETITION c{cn}: {body[:40]!r} appears outside the cloze")
    return issues


def detect_grammar_tell(text: str, groups: dict[str, list[str]]) -> list[str]:
    """Visible 'a' before a vowel-starting answer, or 'an' before consonant."""
    issues = []
    for m in VOWEL_RE.finditer(text):
        article = m.group(1).lower()
        cn = m.group(2)
        body = m.group(3).strip()
        if not body:
            continue
        first = body[0].lower()
        is_vowel = first in "aeiou"
        if article == "a" and is_vowel:
            issues.append(f"GRAMMAR_TELL c{cn}: 'a {body[:20]}...' — answer starts with vowel")
        elif article == "an" and not is_vowel:
            issues.append(f"GRAMMAR_TELL c{cn}: 'an {body[:20]}...' — answer starts with consonant")
    return issues


def check_card(row: dict) -> list[str]:
    text = row.get("text", "")
    groups: dict[str, list[str]] = {}
    for cn, body in CLOZE_RE.findall(text):
        groups.setdefault(cn, []).append(body)
    if len(groups) < 2:
        # Single-cloze cards are out of scope for spoiler heuristics here.
        return []
    issues = []
    issues += detect_numeric_overlap(text, groups)
    issues += detect_numeric_boundary(groups)
    issues += detect_literal_repetition(text, groups)
    issues += detect_grammar_tell(text, groups)
    return issues


def main() -> int:
    strict = "--strict" in sys.argv
    if not CARDS.is_file():
        print(f"{CARDS} not found", file=sys.stderr)
        return 1

    n_total = 0
    n_multi = 0
    flagged: list[tuple[str, str, list[str]]] = []  # (guid, text, issues)

    with CARDS.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_total += 1
            row = json.loads(line)
            issues = check_card(row)
            if issues:
                n_multi += 1
                flagged.append((row.get("guid", "?"), row.get("text", ""), issues))

    print(f"checked {n_total} cards · {len(flagged)} flagged with possible spoiler patterns\n")

    by_kind: dict[str, int] = {}
    for guid, text, issues in flagged:
        for issue in issues:
            kind = issue.split()[0]
            by_kind[kind] = by_kind.get(kind, 0) + 1

    if by_kind:
        print("breakdown:")
        for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<22} {n}")
        print()

    for guid, text, issues in flagged[:30]:
        print(f"  {guid}")
        for issue in issues:
            print(f"    {issue}")
        print(f"    {text[:180]}")
        print()

    if len(flagged) > 30:
        print(f"... + {len(flagged) - 30} more (run with `| head -200` to see all)")

    return 1 if strict and flagged else 0


if __name__ == "__main__":
    sys.exit(main())
