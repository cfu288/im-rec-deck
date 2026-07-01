# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic", "python-dotenv"]
# ///
"""Classify each card in build/cards.jsonl as `is_dosing: true|false`.

A "dosing" card is one whose primary learning value is recalling a specific
dose / regimen / titration number. The user has signaled these are lower-yield
("memorizing dosing for CrCl isn't super helpful"). Classifying lets the
build add an `im-rec-deck::dosing` tag so they can be suspended/filtered.

What counts as DOSING (true):
  - "Standard apixaban for VTE is 10 mg BID × 7 days, then 5 mg BID."
  - "Start metformin at 500 mg daily, titrate to 1000 mg BID over 4 weeks."
  - "Vancomycin trough goal 15-20 mcg/mL for serious MRSA infection."
  - "tPA dose is 0.9 mg/kg IV (max 90 mg), 10% bolus + 90% infusion over 60 min."

What is NOT dosing (false) — concept / criterion / decision rule:
  - "DOACs preferred over warfarin in VTE except in antiphospholipid syndrome."
  - "ACE inhibitors are first-line in HFrEF for mortality benefit."
  - "Apixaban requires renal adjustment when CrCl < 25 mL/min."  (decision rule)
  - "GOLD groups stratify COPD by exacerbation history + symptom burden."

Borderline = false (be conservative). If the card teaches WHEN to dose-adjust,
WHO gets a drug, or WHY a regimen is preferred, it's a concept card.

Cost: cheap Haiku. Sync API, chunked, parallel across chunks.
~$0.03–0.05 per full run on ~1500 cards.

Usage:
    uv run scripts/classify_dosing.py
"""

from __future__ import annotations

import concurrent.futures
import json
import sys
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
CARDS = REPO / "build" / "cards.jsonl"
REPORT = REPO / "build" / "card-classifications.jsonl"
MODEL = "claude-haiku-4-5"
CHUNK_SIZE_CHARS = 25_000  # comfortable for Haiku
MAX_TOKENS = 8000

SYSTEM_PROMPT = """You classify Anki cloze cards as "dosing" or "concept".

A DOSING card teaches a specific number a clinician would normally look up at the bedside: drug dose, infusion rate, titration schedule, max dose, dose-by-weight conversion, taper protocol. The card's whole point is the number.

A CONCEPT card teaches a decision rule, mechanism, indication, contraindication, drug-class choice, classification system, screening criterion, interpretive threshold, or an against-recommendation. These are NOT dosing even if a number appears.

# DOSING (true) — real examples from this deck

  - "Alteplase for AIS is dosed at {{c1::0.9 mg/kg IV}} with a maximum of {{c2::90 mg}}, with {{c3::10%}} given as a bolus over 1 minute and the remainder over 60 minutes."
  - "Carvedilol dosing for portal hypertension: start at {{c1::6.25 mg/day}}, increase to {{c2::12.5 mg/day}} after 2–3 days if tolerated; down-titrate to 6.25 mg/day if SBP < 90 mm Hg."
  - "If parenteral anticoagulation is refused or not tolerated for superficial vein thrombosis, the alternative is rivaroxaban {{c1::10 mg}} orally once daily."
  - "For moderate-to-severe DKA, start a fixed-rate IV insulin infusion at {{c1::0.1 units/kg/h}}; an optional loading dose of {{c1::0.1 units/kg}} IV or SC may precede the infusion."
  - "High-intensity statin regimens consist of {{c1::atorvastatin 40–80 mg}} or {{c1::rosuvastatin 20–40 mg}} daily."

# CONCEPT (false) — real examples from this deck (look-alikes that are NOT dosing)

  - "For most patients with T2D + CKD and eGFR ≥30 ml/min/1.73 m², KDIGO 2022 recommends {{c1::metformin AND an SGLT2i}} as first-line glucose-lowering therapy."
    → drug-class choice / first-line recommendation, not a dose

  - "IV insulin targeting blood glucose {{c1::80–130 mg/dL}} in hospitalized AIS patients is {{c2::not recommended}} (COR 3: No Benefit; SHINE trial)."
    → against-recommendation about a target; the testable fact is the COR-3, not a dose

  - "In SLE/lupus nephritis, {{c1::hydroxychloroquine}} should be prescribed to all patients unless contraindicated."
    → universal indication, not a dose

  - "By Baveno VI criteria, screening EGD can be avoided when LSM < {{c1::20 kPa}} AND platelets > {{c1::150 K/mm³}}."
    → decision rule with diagnostic thresholds, not drug dose

  - "In decompensated cirrhosis, preferred antiviral agents are {{c1::entecavir or TDF}} (or TAF if renal/bone concerns); {{c2::peg-IFN}} is contraindicated."
    → drug choice + contraindication, not dose

  - "Apixaban requires renal adjustment when CrCl < {{c1::25 mL/min}}."
    → threshold for WHEN to adjust, not the adjusted dose itself

# Borderline → CONCEPT

Be conservative. If the card teaches WHEN to dose-adjust, WHO gets a drug, WHY a regimen is preferred, or a diagnostic threshold — it's CONCEPT, not dosing. Only flag true DOSING when the test value is overwhelmingly "remember the exact number/regimen."

# Output

Return ONLY a JSON array, one object per input card, in the same order:
    [{"guid": "...", "is_dosing": true}, {"guid": "...", "is_dosing": false}, ...]

No prose, no markdown fences."""


def chunk_cards(cards: list[dict], max_chars: int) -> list[list[dict]]:
    out: list[list[dict]] = []
    cur: list[dict] = []
    cur_size = 0
    for c in cards:
        size = len(c["text"]) + 60
        if cur and cur_size + size > max_chars:
            out.append(cur)
            cur, cur_size = [], 0
        cur.append(c)
        cur_size += size
    if cur:
        out.append(cur)
    return out


def classify_chunk(client: Anthropic, chunk: list[dict]) -> list[dict]:
    user_msg = "Classify each card:\n\n" + "\n".join(
        f"{i + 1}. guid={c['guid']}: {c['text']}"
        for i, c in enumerate(chunk)
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("["), text.rfind("]")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except json.JSONDecodeError:
                pass
        print(f"  WARNING: bad JSON ({len(text)} chars); skipping chunk", file=sys.stderr)
        return []


def main() -> int:
    load_dotenv(REPO / ".env")
    if not CARDS.is_file():
        print(f"{CARDS} not found", file=sys.stderr)
        return 1

    cards: list[dict] = []
    with CARDS.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cards.append({"guid": row["guid"], "text": row["text"]})

    chunks = chunk_cards(cards, CHUNK_SIZE_CHARS)
    print(f"classifying {len(cards)} cards via {MODEL} in {len(chunks)} chunks…", file=sys.stderr)

    client = Anthropic()
    all_results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(classify_chunk, client, ch) for ch in chunks]
        for i, fut in enumerate(concurrent.futures.as_completed(futs)):
            try:
                all_results.extend(fut.result())
                print(f"  chunk done ({i + 1}/{len(chunks)})", file=sys.stderr)
            except Exception as e:
                print(f"  chunk failed: {e}", file=sys.stderr)

    # Dedupe by guid (last-write-wins, defensive)
    by_guid: dict[str, dict] = {}
    for r in all_results:
        g = r.get("guid")
        if g:
            by_guid[g] = r

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("w") as f:
        for g in sorted(by_guid):
            f.write(json.dumps(by_guid[g], ensure_ascii=False) + "\n")

    n_dosing = sum(1 for r in by_guid.values() if r.get("is_dosing"))
    print(
        f"\nclassified {len(by_guid)}/{len(cards)} cards · "
        f"{n_dosing} dosing ({100 * n_dosing / max(len(by_guid), 1):.1f}%) · "
        f"{len(by_guid) - n_dosing} concept",
        file=sys.stderr,
    )
    print(f"wrote {REPORT.relative_to(REPO)}", file=sys.stderr)

    missing = [c["guid"] for c in cards if c["guid"] not in by_guid]
    if missing:
        print(f"  WARNING: {len(missing)} cards have no classification (parse errors?)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
