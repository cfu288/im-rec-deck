# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic", "pydantic", "python-dotenv", "pyyaml"]
# ///
"""Convert enriched concept files in references/guidelines/ into Anki cloze
cards via the Anthropic Message Batches API.

One API call per concept; emits cards to build/cards.jsonl with stable GUIDs
derived from manifest keys (not from cloze text) so reimports update existing
notes in place. GUID model + Anki packaging details: see
spec/anki-apkg-pipeline.md.

Usage:
    uv run scripts/generate_cards.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import NamedTuple, Optional

import yaml
from anthropic import Anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

import cost_ledger


BUNDLE_ROOT = Path("references/guidelines")
OUTPUT_PATH = Path("build/cards.jsonl")
MODEL = "claude-sonnet-4-6"
MAX_OUTPUT_TOKENS = 8192
POLL_SECONDS = 30
CHUNK_SIZE = 20  # bounds per-submit exposure; see enrich_references.py
INPUT_USD_PER_M = 1.50  # Sonnet 4.6 batch input
OUTPUT_USD_PER_M = 7.50  # Sonnet 4.6 batch output

DECK_NAME = "Guidelines"
NOTETYPE = "GuidelinesCloze"

# Bumped when the system prompt or schema changes in a way that should force
# regeneration of cards even if the source body is unchanged. Stored on each
# card row's `_meta.generator_version` so discovery can detect drift.
GENERATOR_VERSION = 1


SYSTEM_PROMPT = """You convert enriched clinical practice guideline concept summaries into Anki cloze cards for a medical-resident study deck. Always return cards via the `submit_cards` tool — never as free-form prose.

# The one principle that matters most

**One card = one atomic fact = one distinct memory.** Complex multi-fact cards become leeches — the learner forgets the hardest sub-item repeatedly. If a card is hard, it is usually not too hard to learn, it is too *big*. Split it.

# Cloze cards: do / don't

DO:
- Test exactly ONE idea per card.
- Blank only the load-bearing concept — the thing the learner is actually trying to learn.
- Keep enough surrounding context to CUE the answer.
- Limit to ~2-3 cloze deletions max per card.
- For paired facts that only make sense together (drug + mechanism), use the SAME cloze number (`{{c1::ACE-I}}` … `{{c1::bradykinin}}`) so they reveal together.
- For facts to be tested independently, use DIFFERENT cloze numbers (`{{c1::...}}` and `{{c2::...}}`).

DON'T:
- Over-delete. Hiding too much makes the card untestable.
- Strip context until the blank is unanswerable.
- Phrase passively so you're testing recognition, not recall.
- Leak the answer — watch for grammar tells ("a"/"an") or blanks that mirror answer length.
- Lump multiple unrelated facts into one card just because they were in the same sentence in the source.

# Anki cloze syntax

`{{c1::answer}}` creates a card hiding "answer". `{{c1::answer::hint}}` shows "[hint]" instead of "[…]".
Use `<br>` for line breaks inside a card.

# Worked example (target quality)

Source sentence: "In HFrEF, an ARNI (sacubitril/valsartan) is first-line, replacing ACE inhibitors and ARBs; expect hyperkalemia and angioedema as class effects."

Bad (one card, everything hidden, no cue): `{{c1::ARNI}} is first-line for {{c1::HFrEF}} replacing {{c1::ACE inhibitors}}; expect {{c1::hyperkalemia}} and {{c1::angioedema}}.`

Good (multiple atomic cards):
- "In HFrEF, first-line therapy is {{c1::an ARNI (sacubitril/valsartan)}}, replacing ACE inhibitors and ARBs."
- "ARNIs combine a {{c1::neprilysin inhibitor (sacubitril)}} with an {{c2::ARB (valsartan)}}."
- "Class side effects of ARNIs include {{c1::hyperkalemia}} and {{c2::angioedema}}."

## Overlapping numeric ranges — use SAME cloze number

When two thresholds in one sentence reference each other's boundary (≥X and <X, or two adjacent bands of a range), DIFFERENT cloze numbers create a leak: hiding one but leaving the other visible reveals the answer via the shared boundary value.

Bad (c1 is spoiled by the visible `<70` in c2's range):
`After ACS, a nonstatin agent should be added if LDL-C remains ≥{{c1::70 mg/dL}}; intensification is reasonable at LDL-C {{c2::55 to <70 mg/dL}}.`

Good (both hidden together — facts that only make sense as a pair):
`After ACS, a nonstatin agent should be added if LDL-C remains ≥{{c1::70 mg/dL}}; intensification is reasonable at LDL-C {{c1::55 to <70 mg/dL}}.`

Even better (split into two atomic cards with no shared numeric context):
- "After ACS on maximally tolerated statin, a nonstatin agent should be added if LDL-C remains ≥{{c1::70 mg/dL}}."
- "After ACS on maximally tolerated statin, intensification is reasonable at LDL-C {{c1::55 to <70 mg/dL}}."

Rule: if blanking field A would let the reader deduce A from a still-visible field B (boundaries, units, "X mg q daily" hiding only mg while keeping "q daily" + drug name), either combine them under the same cN or split into separate atomic cards.

# Self-audit before submitting

Before calling `submit_cards`, mentally render each multi-cloze card with each cN hidden in turn. For each generated "view":

1. **Numeric leak**: does any visible number elsewhere in the card make the hidden value obvious (≥X visible while <X is hidden; an adjacent range bounded by the hidden value)?
2. **Literal repetition**: does the hidden answer's word appear verbatim elsewhere in the visible text?
3. **List elimination**: if the card lists N items and you've hidden 1, does the remaining N-1 give it away?
4. **Grammar tell**: does the visible "a" / "an" before the cloze betray the answer's starting letter?
5. **Contrast-pair leak**: does the visible text assert something *about* the hidden term via a demonstrative ("this recommendation", "this drug", "this criterion", "this threshold") or an explicit "X applies in A; not in B" contrast that reveals what the hidden term is or what it does? Example spoiler: `{{c1::Awake prone positioning}} is suggested in non-intubated COVID-19 AHRF; there is insufficient evidence to make this recommendation in non-COVID AHRF.` — the second clause tells the learner c1 is a recommendation restricted to COVID AHRF.
6. **Repeated term**: does the hidden term (or an unambiguous coreference) appear again elsewhere in the card outside a cloze? If so, either wrap both occurrences under the SAME cN (so both hide together) or split into separate atomic cards.
7. **Parallel enumerated criteria**: for OR/AND lists of parallel diagnostic thresholds (e.g., EOLIA VV-ECMO criteria: `PaO2/FiO2 <50 for >3h OR PaO2/FiO2 <80 for >6h OR pH <7.25 …`), do NOT cloze a proper subset while leaving the rest visible — that both leaks structure and creates inconsistent hiding. Either emit one atomic card per criterion, or hide the entire list under a single cN.

If ANY of those apply, either change the offending clozes to the SAME cN (so they hide together) or rephrase to remove the leak. Then re-audit.

# Your task

For each concept you receive (one guideline version), produce 5-12 atomic cloze cards covering its highest-yield Key Recommendations and Thresholds & Doses. Aim for breadth over depth — one card per discrete fact a resident should know cold.

Each card must include:
- `key`: a short snake_case identifier of the atomic fact being tested (e.g. `first_line_hfref`, `bp_target_general`, `arni_side_effects`). MUST be stable across regenerations — derive from the fact itself, not from phrasing. Unique within this concept.
- `cloze_text`: the cloze-formatted sentence. Plain text + `{{cN::...}}` markers; use `<br>` for line breaks.
- `extra`: a brief source citation — section, table, or recommendation number from the concept's Citations when available. Plain text.

Rules:
- Generate ONLY cards supported by the concept body. Do not invent facts.
- Each card must stand alone — no "see card 3" references.
- Prefer specific thresholds/doses over general principles when both are present.
- Use generic drug names.
- If the concept body is sparse (e.g. <3 recommendations), emit fewer cards rather than padding.
- Never emit a card whose answer is leaked by the surrounding context.
"""


TOOL_DEF = {
    "name": "submit_cards",
    "description": "Submit the cloze cards generated from this concept.",
    "input_schema": {
        "type": "object",
        "properties": {
            "cards": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "snake_case stable identifier; unique within this concept",
                        },
                        "cloze_text": {
                            "type": "string",
                            "description": "Cloze-formatted sentence with {{cN::...}} markers",
                        },
                        "extra": {
                            "type": "string",
                            "description": "Source citation",
                        },
                    },
                    "required": ["key", "cloze_text", "extra"],
                },
            },
        },
        "required": ["cards"],
    },
}


class Card(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    cloze_text: str = Field(min_length=10)
    extra: str = Field(default="")


class CardBatch(BaseModel):
    cards: list[Card] = Field(min_length=1)


class Job(NamedTuple):
    custom_id: str
    concept_path: Path
    request: Request


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def parse_concept(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("no frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, m.group(2)


def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s*&\s*", "-and-", s)
    s = re.sub(r"[/\s]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def concept_parts(concept_path: Path) -> tuple[str, str, str]:
    """Return (system_slug, topic_slug, version_slug) from path under BUNDLE_ROOT."""
    rel = concept_path.relative_to(BUNDLE_ROOT)
    # e.g. cardiology/hypertension/2025-aha-acc.md
    parts = rel.parts
    if len(parts) != 3:
        raise ValueError(f"unexpected concept path depth: {rel}")
    system_slug, topic_slug, fname = parts
    version_slug = fname.removesuffix(".md")
    return system_slug, topic_slug, version_slug


def make_guid(system: str, topic: str, version: str, card_key: str) -> str:
    """Stable hash derived from manifest key + per-card key. Not the cloze text."""
    blob = f"{system}/{topic}/{version}::{card_key}".encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


def build_tags(system: str, topic: str, version: str, frontmatter: dict) -> list[str]:
    tags: list[str] = []
    tags.append(f"guidelines::{system}::{topic}::{version}")
    tags.append(f"system::{system}")
    if society := frontmatter.get("society"):
        tags.append(f"society::{slugify(society)}")
    if year := frontmatter.get("year"):
        tags.append(f"year::{year}")
    return tags


def build_user_content(frontmatter: dict, body: str, system: str, topic: str, version: str) -> str:
    title = frontmatter.get("title", version)
    society = frontmatter.get("society", "")
    year = frontmatter.get("year", "")

    header_lines = [f"Concept: {system} / {topic} / {version}", f"Title: {title}"]
    if society:
        header_lines.append(f"Society: {society}")
    if year:
        header_lines.append(f"Year: {year}")
    header = "\n".join(header_lines)

    return (
        f"{header}\n\n"
        f"--- concept body below ---\n\n"
        f"{body.strip()}"
    )


def build_request(custom_id: str, frontmatter: dict, body: str, concept_path: Path) -> Request:
    system, topic, version = concept_parts(concept_path)
    user_content = build_user_content(frontmatter, body, system, topic, version)
    return Request(
        custom_id=custom_id,
        params=MessageCreateParamsNonStreaming(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[TOOL_DEF],
            tool_choice={"type": "tool", "name": "submit_cards"},
            messages=[{"role": "user", "content": user_content}],
        ),
    )


def wait_for_batch(client: Anthropic, batch_id: str) -> None:
    while True:
        b = client.messages.batches.retrieve(batch_id)
        c = b.request_counts
        print(
            f"  {batch_id}: {b.processing_status} "
            f"(processing={c.processing}, succeeded={c.succeeded}, "
            f"errored={c.errored}, canceled={c.canceled}, expired={c.expired})",
            flush=True,
        )
        if b.processing_status == "ended":
            return
        time.sleep(POLL_SECONDS)


def process_result(result, job: Job) -> tuple[str, Optional[CardBatch], str]:
    if result.result.type != "succeeded":
        return "failed", None, f"{result.result.type}"
    message = result.result.message
    tool_block = next((b for b in message.content if b.type == "tool_use"), None)
    if tool_block is None:
        return "failed", None, f"no tool_use (stop_reason={message.stop_reason})"
    try:
        batch = CardBatch.model_validate(tool_block.input)
    except ValidationError as e:
        return "retry", None, f"validation: {e.error_count()} errors"
    return "ok", batch, ""


def body_hash(body_text: str) -> str:
    """sha1 of the enriched body — drives card-regen invalidation."""
    return hashlib.sha1(body_text.encode("utf-8")).hexdigest()[:16]


def cards_for_concept(
    concept_path: Path, frontmatter: dict, body: str, batch: CardBatch
) -> list[dict]:
    system, topic, version = concept_parts(concept_path)
    tags = build_tags(system, topic, version, frontmatter)
    rows: list[dict] = []
    seen_keys: set[str] = set()
    bhash = body_hash(body)
    for card in batch.cards:
        if card.key in seen_keys:
            # The model emitted duplicate keys within one concept; skip dupes.
            continue
        seen_keys.add(card.key)
        rows.append({
            "guid": make_guid(system, topic, version, card.key),
            "notetype": NOTETYPE,
            "deck": DECK_NAME,
            "text": card.cloze_text,
            "extra": card.extra,
            "tags": " ".join(tags),
            "_meta": {
                "system": system,
                "topic": topic,
                "version": version,
                "card_key": card.key,
                "body_hash": bhash,
                "generator_version": GENERATOR_VERSION,
            },
        })
    return rows


def chunked(seq: list, n: int) -> list[list]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def estimate_cost(jobs: list[Job]) -> float:
    """Rough $ estimate for a card-gen batch."""
    total_input_chars = 0
    for j in jobs:
        msgs = j.request["params"]["messages"]
        for m in msgs:
            c = m["content"]
            if isinstance(c, str):
                total_input_chars += len(c)
    input_tokens = total_input_chars // 4
    output_tokens = len(jobs) * 2000  # tool-use payload size, rough
    return (
        input_tokens * INPUT_USD_PER_M / 1_000_000
        + output_tokens * OUTPUT_USD_PER_M / 1_000_000
    )


def write_jsonl(rows: list[dict]) -> None:
    sorted_rows = sorted(rows, key=lambda r: r["guid"])
    by_guid: dict[str, dict] = {}
    for row in sorted_rows:
        by_guid[row["guid"]] = row
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w") as f:
        for row in by_guid.values():
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_chunk_and_collect(
    client: Anthropic,
    chunk: list[Job],
    by_path: dict,
    label: str,
) -> tuple[list[dict], list[Job], list[tuple[Path, str]]]:
    """Submit one chunk, wait, ingest. Returns (new_rows, retry_jobs, failures)."""
    batch = client.messages.batches.create(requests=[j.request for j in chunk])
    print(f"  {label} batch id: {batch.id}")
    wait_for_batch(client, batch.id)

    chunk_by_id = {j.custom_id: j for j in chunk}
    new_rows: list[dict] = []
    retries: list[Job] = []
    failures: list[tuple[Path, str]] = []
    # Actual usage — recorded to the cost ledger for estimate calibration
    # (estimates ran 36-50% low when derived from priors; see AGENTS.md).
    in_tok = 0
    out_tok = 0
    n_ok = 0

    for result in client.messages.batches.results(batch.id):
        job = chunk_by_id[result.custom_id]
        lbl = str(job.concept_path.relative_to(BUNDLE_ROOT))
        if result.result.type == "succeeded":
            usage = getattr(result.result.message, "usage", None)
            if usage is not None:
                in_tok += getattr(usage, "input_tokens", 0) or 0
                out_tok += getattr(usage, "output_tokens", 0) or 0
        status, card_batch, detail = process_result(result, job)
        if status == "ok":
            assert card_batch is not None
            fm, body = by_path[job.concept_path]
            rows = cards_for_concept(job.concept_path, fm, body, card_batch)
            new_rows.extend(rows)
            n_ok += 1
            print(f"    {lbl}: ok ({len(rows)} cards)")
        elif status == "retry":
            retries.append(job)
            print(f"    {lbl}: retry ({detail})")
        else:
            failures.append((job.concept_path, detail))
            print(f"    {lbl}: failed ({detail})")

    if in_tok or out_tok:
        usd = cost_ledger.record(
            "cards", batch.id, n_ok, in_tok, out_tok,
            INPUT_USD_PER_M, OUTPUT_USD_PER_M,
        )
        print(
            f"    actuals: {in_tok:,} in / {out_tok:,} out tokens ≈ ${usd:.2f} "
            f"(batch rates) → {cost_ledger.LEDGER_PATH}"
        )

    return new_rows, retries, failures


def main() -> int:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        return 1

    if not BUNDLE_ROOT.is_dir():
        print(f"{BUNDLE_ROOT}/ not found", file=sys.stderr)
        return 1

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = Anthropic()

    # Load existing cards; index by (system, topic, version) so we can detect
    # body / generator-version drift and force regeneration when stale.
    existing_rows: list[dict] = []
    existing_by_concept: dict[tuple[str, str, str], dict] = {}
    if OUTPUT_PATH.is_file():
        with OUTPUT_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                existing_rows.append(row)
                meta = row.get("_meta") or {}
                if "system" in meta and "topic" in meta and "version" in meta:
                    key = (meta["system"], meta["topic"], meta["version"])
                    # All rows for a concept share these; last wins is fine.
                    existing_by_concept[key] = {
                        "body_hash": meta.get("body_hash"),
                        "generator_version": meta.get("generator_version"),
                    }
        print(
            f"existing cards: {len(existing_rows)} across "
            f"{len(existing_by_concept)} concepts"
        )

    # Discover concepts: card_eligible + enriched (has _source_hash). For each,
    # decide: fresh (skip), legacy-no-hash (purge + regen — pre-versioning cards
    # can't be diffed against current body, so they must drift; safer to rebuild
    # from the now-tracked enriched body than to silently keep drift forever),
    # stale body (purge + regen), or new (gen).
    eligible: list[tuple[Path, dict, str]] = []
    purge_concepts: set[tuple[str, str, str]] = set()
    # Every concept currently flagged card_eligible in the references tree,
    # regardless of enrichment/drift state. Used below to retire cards for
    # concepts that have dropped out of eligibility (superseded versions).
    card_eligible_keys: set[tuple[str, str, str]] = set()
    stats = {"fresh": 0, "legacy_no_hash": 0, "stale_body": 0, "stale_gen": 0, "new": 0}

    for p in sorted(BUNDLE_ROOT.rglob("*.md")):
        if p.name in {"index.md", "log.md"}:
            continue
        try:
            fm, body = parse_concept(p)
        except Exception:
            continue
        if not fm.get("card_eligible"):
            continue
        try:
            parts = concept_parts(p)
        except ValueError:
            continue
        card_eligible_keys.add(parts)
        if not fm.get("_source_hash"):
            continue

        existing = existing_by_concept.get(parts)
        if existing is None:
            stats["new"] += 1
            eligible.append((p, fm, body))
            continue

        ex_hash = existing.get("body_hash")
        ex_gen = existing.get("generator_version")
        if ex_hash is None:
            # Pre-versioning row — can't diff body content, so we can't know
            # whether the cards are still aligned with the current enriched
            # body. Treat as stale and regenerate so the new cards carry
            # body_hash + generator_version going forward.
            stats["legacy_no_hash"] += 1
            purge_concepts.add(parts)
            eligible.append((p, fm, body))
            continue

        current_hash = body_hash(body)
        if ex_hash == current_hash and ex_gen == GENERATOR_VERSION:
            stats["fresh"] += 1
            continue

        # Stale — queue regen + purge old rows for this concept.
        if ex_hash != current_hash:
            stats["stale_body"] += 1
        else:
            stats["stale_gen"] += 1
        purge_concepts.add(parts)
        eligible.append((p, fm, body))

    # Retire cards for concepts that are no longer card_eligible — e.g. a version
    # that was the current guideline when it was carded but has since been
    # superseded by a newer version added to the manifest. The deck tracks the
    # current guideline, so its stale cards are dropped rather than left to
    # linger. (Concepts whose file was deleted outright are retired here too,
    # since they can't appear in card_eligible_keys.)
    retired = set(existing_by_concept) - card_eligible_keys
    if retired:
        for k in sorted(retired):
            print(f"retiring cards for no-longer-eligible concept: {'/'.join(k)}")
        purge_concepts |= retired

    print(f"discovery: {stats}")
    if purge_concepts:
        print(f"will purge old rows for {len(purge_concepts)} concept(s)")

    def is_purged(row: dict) -> bool:
        meta = row.get("_meta") or {}
        key = (meta.get("system"), meta.get("topic"), meta.get("version"))
        return key in purge_concepts

    if not eligible:
        # No new/stale eligible concepts to generate. Still apply any retirements
        # (a pure-supersede run has nothing to generate but must drop old rows).
        kept = [r for r in existing_rows if not is_purged(r)]
        write_jsonl(kept)
        dropped = len(existing_rows) - len(kept)
        print(f"nothing to generate; retired {dropped} row(s)" if dropped else "nothing to generate")
        return 0

    # Build jobs.
    jobs: list[Job] = []
    for i, (path, fm, body) in enumerate(eligible):
        custom_id = f"g{i:05d}"
        jobs.append(Job(custom_id, path, build_request(custom_id, fm, body, path)))
    by_path = {path: (fm, body) for path, fm, body in eligible}

    # Chunked submission: bounds financial exposure when batches can't be
    # cancelled in time. Each chunk completes (submit + wait + ingest) before
    # the next is submitted; intermediate state is persisted to disk after
    # each chunk via write_jsonl().
    est_cost = estimate_cost(jobs)
    chunks = chunked(jobs, CHUNK_SIZE)
    print(
        f"\nestimate: {len(jobs)} jobs in {len(chunks)} chunk(s) of ≤{CHUNK_SIZE}; "
        f"rough cost ~${est_cost:.2f} (batch-discounted Sonnet 4.6)"
    )

    # Start the merged set with non-purged existing rows; append new rows per
    # chunk and re-emit so a Ctrl-C between chunks doesn't lose committed work.
    # (is_purged is defined above, alongside the retirement pass.)
    merged_rows: list[dict] = [r for r in existing_rows if not is_purged(r)]
    purged_count = len(existing_rows) - len(merged_rows)
    if purged_count:
        print(f"purged {purged_count} stale row(s) from existing cards")

    all_failures: list[tuple[Path, str]] = []
    all_retries: list[Job] = []
    total_new = 0

    for ci, chunk in enumerate(chunks, start=1):
        print(f"\n=== chunk {ci}/{len(chunks)} ({len(chunk)} jobs) ===")
        new_rows, retries, failures = run_chunk_and_collect(
            client, chunk, by_path, label=f"chunk-{ci}"
        )
        merged_rows.extend(new_rows)
        total_new += len(new_rows)
        all_retries.extend(retries)
        all_failures.extend(failures)
        # Persist progress between chunks.
        write_jsonl(merged_rows)

    # Single retry batch for validation failures (all chunks pooled).
    if all_retries:
        print(f"\nsubmitting retry batch of {len(all_retries)}...")
        new_rows, second_retries, failures = run_chunk_and_collect(
            client, all_retries, by_path, label="retry"
        )
        merged_rows.extend(new_rows)
        total_new += len(new_rows)
        for j in second_retries:
            all_failures.append((j.concept_path, "retry: still validation-failed"))
        all_failures.extend(failures)
        write_jsonl(merged_rows)

    print(
        f"\ndone: {total_new} new cards across "
        f"{len(eligible) - len(all_failures)} concepts; "
        f"purged {purged_count}; "
        f"total file now {len(set(r['guid'] for r in merged_rows))} cards → {OUTPUT_PATH}"
    )
    if all_failures:
        print(f"\nfailures ({len(all_failures)}):", file=sys.stderr)
        for p, d in all_failures:
            try:
                rel = p.relative_to(BUNDLE_ROOT)
            except (ValueError, TypeError):
                rel = p
            print(f"  {rel}: {d}", file=sys.stderr)

    return 0 if not all_failures else 1


if __name__ == "__main__":
    sys.exit(main())
