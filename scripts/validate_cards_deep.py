# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic", "python-dotenv"]
# ///
"""LLM-based deep evaluation of multi-cloze cards for spoiler patterns.

Catches semantic / conceptual leaks the heuristic validator misses
(e.g. inherent pairing, list-elimination spoilers, contextual hints).

Uses Claude Haiku (cheap). Sync API, chunked by system, parallelized across
systems. Cost: ~$0.10-0.20 per full run depending on card count.

Usage:
    uv run scripts/validate_cards_deep.py             # run + write report
    uv run scripts/validate_cards_deep.py --strict    # exit non-zero if any flagged
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
CARDS = REPO / "build" / "cards.jsonl"
REPORT = REPO / "build" / "qa-cards-deep.jsonl"
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 8000

CLOZE_RE = re.compile(r"\{\{c(\d+)::")


SYSTEM_PROMPT = """You audit Anki multi-cloze cards for SPOILER patterns: cases where one cloze's answer is leaked by the surrounding text or by another cloze's body that's still visible on the generated card.

# Background
Anki creates one card per distinct cloze number. So `{{c1::A}} ... {{c2::B}}` makes 2 cards:
- Card 1: shows "[...] ... B" → user must recall A
- Card 2: shows "A ... [...]" → user must recall B

# Real spoiler patterns to flag

1. **Numeric boundary leak**: c1 has `≥X` while c2 contains `<X` (or a range bounded by X). Hiding c1 leaves "<X" visible elsewhere → user trivially derives X.
2. **Literal text repetition**: c1's answer word appears verbatim in the surrounding visible text (or another cloze body).
3. **Mutually-exclusive list members**: c1 and c2 are two items of a 2- or 3-item list where the stem makes elimination trivial.
4. **Paired single-fact split**: c1 + c2 are two halves of one inseparable rule ("SBP >180 mmHg or DBP >110 mmHg" as ONE contraindication; "vasoreactivity: ≥10 mmHg AND ≤40 mmHg" as ONE criterion).
5. **Grammar tell**: visible "a" before a vowel-starting answer or "an" before a consonant.

# What is NOT a spoiler (don't flag)

- Independent thresholds for different measurements (e.g., different drugs, different lab values that don't relate)
- Numeric thresholds that happen to share digits but aren't actually the same value
- "Compare/contrast" pairs where the contrast IS the testable fact (LMWH vs UFH, fidaxomicin vs vancomycin) — naming one doesn't give away the other
- Adjacent guideline-year facts (e.g., "USPSTF lowered CRC age from 50 to 45")

# Decision per card

For each input card, emit JSON with either:
- `{"guid": "...", "ok": true}` if no spoiler
- `{"guid": "...", "issue": "<concise reason>", "kind": "<one of: numeric_boundary | literal_repetition | list_elimination | paired_single_fact | grammar_tell | other>"}` if spoiled

Be conservative. When in doubt, OK. Only flag clear spoilers.

# Output format

Return ONLY a JSON array of these objects, one per input card, in the same order. No prose, no markdown fences. Start with `[` and end with `]`.
"""


def chunk_cards(cards: list[dict], max_chars: int = 30_000) -> list[list[dict]]:
    """Split a list of cards into chunks small enough to fit Haiku's context comfortably."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    cur_size = 0
    for c in cards:
        size = len(c["text"]) + 80  # approximate prompt overhead per card
        if cur and cur_size + size > max_chars:
            out.append(cur)
            cur, cur_size = [], 0
        cur.append(c)
        cur_size += size
    if cur:
        out.append(cur)
    return out


def eval_chunk(client: Anthropic, chunk: list[dict]) -> list[dict]:
    """Send one chunk of cards to Haiku, parse JSON response."""
    user_msg = "Cards to evaluate:\n\n" + "\n".join(
        f"{i + 1}. guid={c['guid']}\n   text: {c['text']}"
        for i, c in enumerate(chunk)
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    # Strip any stray markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    # Fall back: try to extract a top-level array
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    print(f"  WARNING: failed to parse JSON from response ({len(text)} chars)", file=sys.stderr)
    return []


def eval_system(client: Anthropic, system: str, cards: list[dict]) -> list[dict]:
    chunks = chunk_cards(cards)
    results: list[dict] = []
    for chunk in chunks:
        results.extend(eval_chunk(client, chunk))
    print(f"  {system:<25} {len(cards):4d} cards · {len(chunks)} chunk(s)", file=sys.stderr)
    return results


def main() -> int:
    load_dotenv()
    if not CARDS.is_file():
        print(f"{CARDS} not found — run `just cards` first", file=sys.stderr)
        return 1

    strict = "--strict" in sys.argv

    # Bucket multi-cloze cards by system
    buckets: dict[str, list[dict]] = defaultdict(list)
    with CARDS.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            ns = set(CLOZE_RE.findall(row["text"]))
            if len(ns) < 2:
                continue
            system = (row.get("_meta") or {}).get("system", "unknown")
            buckets[system].append({"guid": row["guid"], "text": row["text"]})

    n_multi = sum(len(v) for v in buckets.values())
    if not n_multi:
        print("no multi-cloze cards found")
        return 0

    print(f"evaluating {n_multi} multi-cloze cards across {len(buckets)} systems via {MODEL}…", file=sys.stderr)

    client = Anthropic()
    all_results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = {
            ex.submit(eval_system, client, sys, cs): sys
            for sys, cs in buckets.items()
        }
        for fut in concurrent.futures.as_completed(futs):
            try:
                all_results.extend(fut.result())
            except Exception as e:
                sys_name = futs[fut]
                print(f"  ERROR in {sys_name}: {e}", file=sys.stderr)

    # Filter to flagged only
    flagged = [r for r in all_results if not r.get("ok")]

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w") as f:
        for r in flagged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    by_kind: dict[str, int] = {}
    for r in flagged:
        k = r.get("kind", "other")
        by_kind[k] = by_kind.get(k, 0) + 1

    print(f"\nchecked {n_multi} multi-cloze cards · {len(flagged)} flagged")
    if by_kind:
        for k, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print(f"  {k:<22} {n}")
    print(f"\nfull report: {REPORT.relative_to(REPO)}")

    return 1 if strict and flagged else 0


if __name__ == "__main__":
    sys.exit(main())
