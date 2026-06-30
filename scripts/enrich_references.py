# /// script
# requires-python = ">=3.10"
# dependencies = ["anthropic", "pydantic", "python-dotenv", "pyyaml"]
# ///
"""Enrich card-eligible concept files in references/guidelines/ with structured
bodies extracted from their parsed-source markdown via the Anthropic Message
Batches API (50% cost discount).

Idempotent: each concept stores a sha256 of its source under `_source_hash`;
reruns skip concepts whose source hasn't changed.

Usage:
    uv run scripts/enrich_references.py
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

import yaml
from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    InternalServerError,
)
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError


BUNDLE_ROOT = Path("references/guidelines")
STATE_PATH = Path("build/.enrich-state.json")

# Chunked submission bounds the financial exposure when a batch can't be
# cancelled in time (Anthropic processes most queued requests before a cancel
# lands). Each chunk completes before the next is submitted; state-file resume
# already handles Ctrl-C between chunks.
CHUNK_SIZE = 20
# Opus 4.7 batch pricing (50% off standard $5 / $25 per M tokens).
# Note: Opus 4.7 uses a new tokenizer that can produce up to ~35% more tokens
# for the same source text vs. Sonnet 4.6 — factor that into pre-run estimates.
# Tier-up from Sonnet 4.6: enrichment is the upstream knowledge layer that
# cards + docs summaries both derive from, so accuracy here propagates
# downstream. Run-once cost; references/ is committed afterwards.
INPUT_USD_PER_M = 2.50
OUTPUT_USD_PER_M = 12.50
MODEL = "claude-opus-4-7"
MAX_OUTPUT_TOKENS = 8192
MAX_SOURCE_CHARS = 2_000_000        # ~660K tokens at ~3 chars/token (Opus 4.7);
                                    # covers 100% of current sources (max 1.23M
                                    # chars) with headroom. Opus 4.7 has 1M ctx,
                                    # so per-request input stays under budget.
HEAD_CHARS = 1_600_000              # 80% head — matches original head/tail ratio
TAIL_CHARS = 400_000                # 20% tail (only used when len > MAX_SOURCE_CHARS)
POLL_SECONDS = 30
SOURCE_FORMAT_PREFERENCE = ("epub", "html", "pdf")  # match parse_sources.py

SYSTEM_PROMPT = """You extract structured study material from clinical practice guideline source documents for an Anki-flashcard generation pipeline.

You will be given:
1. Metadata about a single named guideline (title, society, year).
2. The parsed full text of the guideline document.

Your task: produce a concise OKF (Open Knowledge Format) concept body and return it via the `submit_enriched_body` tool. Always use the tool — do not return free-form prose.

Field definitions:
- `summary`: 2-4 sentences. What this guideline covers and, if the source mentions it, what's new vs the prior version. Plain English, no bullets, no markdown.
- `key_recommendations`: 5-12 bulleted items. The highest-yield clinical decisions a resident would be expected to know on rounds: drug class choice, threshold for intervention, first-line therapy, monitoring intervals. Each bullet is one sentence. Prefer specific recommendations over general principles.
- `thresholds_and_doses`: bulleted items containing concrete numerics — BP targets, drug doses, lab cutoffs, screening intervals, age cutoffs. Each bullet states the threshold and what triggers it. Leave the array EMPTY if the guideline does not contain numeric thresholds.
- `citations`: 3-8 bullets. Each citation should reference a specific section name, recommendation number, table, or page when the source provides one. Format: "<section/figure/page> — <what it supports>". If the source has no explicit section numbers, cite by topic heading.

Style rules:
- Be terse. Telegraphic when safe. Each bullet stands alone.
- Use generic drug names. Use the guideline's exact threshold values and units.
- Do not invent recommendations. If the source does not say it, do not include it.
- Do not include prose like "The guideline recommends..." — just state the recommendation.
- No disclaimers, no intros, no "consult your physician" boilerplate.
"""

TOOL_DEF = {
    "name": "submit_enriched_body",
    "description": "Submit the structured body for this guideline concept.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "2-4 sentence prose summary.",
            },
            "key_recommendations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "5-12 single-sentence high-yield clinical decisions.",
            },
            "thresholds_and_doses": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bulleted numerics. Empty array if the guideline contains none.",
            },
            "citations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-8 source citations.",
            },
        },
        "required": [
            "summary",
            "key_recommendations",
            "thresholds_and_doses",
            "citations",
        ],
    },
}


class EnrichedBody(BaseModel):
    summary: str = Field(min_length=20)
    key_recommendations: list[str] = Field(min_length=1)
    thresholds_and_doses: list[str] = Field(default_factory=list)
    citations: list[str] = Field(min_length=1)


class Job(NamedTuple):
    custom_id: str
    path: Path
    frontmatter: dict
    source_hash: str
    request: Request


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def parse_concept(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("no frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, m.group(2)


def write_concept(path: Path, frontmatter: dict, body: str) -> None:
    yaml_block = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True
    ).strip()
    path.write_text(f"---\n{yaml_block}\n---\n\n{body.rstrip()}\n")


def pick_source(frontmatter: dict) -> Optional[Path]:
    """Return the repo-relative path of the best available parsed-source .md."""
    source = frontmatter.get("source") or {}
    for fmt in SOURCE_FORMAT_PREFERENCE:
        entry = source.get(fmt) or {}
        local = entry.get("local")
        if not local:
            continue
        # source.<fmt>.local is bundle-relative starting with /; strip to repo-relative
        original = Path(local.lstrip("/"))
        sidecar = original.with_suffix(".md")
        if sidecar.is_file():
            return sidecar
    return None


def truncate_source(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_SOURCE_CHARS:
        return text, False
    head = text[:HEAD_CHARS]
    tail = text[-TAIL_CHARS:]
    return f"{head}\n\n...[truncated middle of source]...\n\n{tail}", True


def assemble_body(b: EnrichedBody) -> str:
    parts: list[str] = []
    parts.append("# Summary")
    parts.append("")
    parts.append(b.summary.strip())
    parts.append("")
    parts.append("# Key Recommendations")
    parts.append("")
    for item in b.key_recommendations:
        parts.append(f"- {item.strip()}")
    if b.thresholds_and_doses:
        parts.append("")
        parts.append("# Thresholds & Doses")
        parts.append("")
        for item in b.thresholds_and_doses:
            parts.append(f"- {item.strip()}")
    parts.append("")
    parts.append("# Citations")
    parts.append("")
    for item in b.citations:
        parts.append(f"- {item.strip()}")
    return "\n".join(parts) + "\n"


def build_request(custom_id: str, frontmatter: dict, source_text: str, source_path: Path) -> Request:
    truncated_text, was_truncated = truncate_source(source_text)
    if was_truncated:
        print(
            f"  truncating {source_path} ({len(source_text):,} chars)",
            file=sys.stderr,
        )

    metadata_lines = [f"Title: {frontmatter.get('title', source_path.stem)}"]
    if society := frontmatter.get("society"):
        metadata_lines.append(f"Society: {society}")
    if year := frontmatter.get("year"):
        metadata_lines.append(f"Year: {year}")
    metadata_block = "\n".join(metadata_lines)

    user_content = (
        f"Concept metadata:\n{metadata_block}\n\n"
        f"Parsed source document:\n\n{truncated_text}"
    )

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
            tool_choice={"type": "tool", "name": "submit_enriched_body"},
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


# ────────────────────────────────────────────────────────────────────────────
# State file: tracks every submitted batch + its custom_id → concept mapping
# so a crashed / canceled / interrupted run is recovered on next invocation.
# Replaces the standalone salvage_batch.py — recovery is built-in now.
# ────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if not STATE_PATH.is_file():
        return {"batches": []}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        # Corrupt state — start fresh rather than crash. Existing _source_hash
        # markers on concepts protect against re-spending.
        print(f"warning: {STATE_PATH} corrupt; ignoring", file=sys.stderr)
        return {"batches": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(STATE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def chunked(seq: list, n: int) -> list[list]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def estimate_cost(jobs: list[Job]) -> tuple[float, int]:
    """Rough $ estimate for a batch + total input tokens.

    Uses ~3 chars/token because Opus 4.7's new tokenizer produces up to ~35%
    more tokens than prior Claude models for the same text (per docs.claude.com).
    Adjust if the model is downshifted to a pre-Opus-4.7 generation.
    """
    total_input_chars = 0
    for j in jobs:
        msgs = j.request["params"]["messages"]
        for m in msgs:
            c = m["content"]
            if isinstance(c, str):
                total_input_chars += len(c)
    input_tokens = total_input_chars // 3
    # Assume ~1500 output tokens per call (typical for tool-use payload).
    output_tokens = len(jobs) * 1500
    cost = (
        input_tokens * INPUT_USD_PER_M / 1_000_000
        + output_tokens * OUTPUT_USD_PER_M / 1_000_000
    )
    return cost, input_tokens


def submit_and_track(
    client: Anthropic, jobs: list[Job], state: dict, label: str
) -> dict:
    requests = [j.request for j in jobs]
    # The Anthropic SDK retries internally, but batch-submission has been
    # observed to fail with sustained Cloudflare 502s / dropped connections
    # during regional incidents. Wrap with explicit exponential backoff so a
    # 5-30min infrastructure blip doesn't lose the whole resume-state context.
    backoffs = [15, 60, 180, 600]
    last_exc: Exception | None = None
    for attempt, wait in enumerate([0] + backoffs):
        if wait:
            print(f"  submit retry {attempt}/{len(backoffs)} after {wait}s …")
            time.sleep(wait)
        try:
            batch = client.messages.batches.create(requests=requests)
            break
        except (APIConnectionError, InternalServerError, APIStatusError) as e:
            last_exc = e
            print(f"  submit attempt {attempt + 1} failed: {type(e).__name__}: {e}")
    else:
        raise last_exc  # all retries exhausted
    print(f"submitted {label} batch {batch.id} ({len(jobs)} requests)")
    entry = {
        "id": batch.id,
        "label": label,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "ingested": False,
        "concepts": {
            j.custom_id: {
                "path": str(j.path),
                "source_hash": j.source_hash,
            }
            for j in jobs
        },
    }
    state["batches"].append(entry)
    save_state(state)
    return entry


def rebuild_job_for_retry(
    custom_id: str, concept_path: Path, source_hash: str
) -> Optional[Job]:
    """Reconstruct a Job for a retry batch by re-reading the concept + source."""
    try:
        fm, _ = parse_concept(concept_path)
    except Exception:
        return None
    source_path = pick_source(fm)
    if source_path is None:
        return None
    source_text = source_path.read_text()
    return Job(
        custom_id,
        concept_path,
        fm,
        source_hash,
        build_request(custom_id, fm, source_text, source_path),
    )


def ingest_batch(
    client: Anthropic, entry: dict
) -> tuple[int, list[tuple[str, Path, str]], list[tuple[Path, str]]]:
    """Process a (presumed-ended) batch's results. Writes succeeded concepts to
    disk; returns (ok_count, retry_candidates, failed).

    retry_candidates is a list of (custom_id, path, source_hash) tuples for
    concepts whose response failed Pydantic validation — eligible for one
    re-submission.
    """
    ok = 0
    retries: list[tuple[str, Path, str]] = []
    failed: list[tuple[Path, str]] = []

    for result in client.messages.batches.results(entry["id"]):
        m = entry["concepts"].get(result.custom_id)
        if not m:
            failed.append((Path(result.custom_id), "unknown custom_id in state"))
            continue
        path = Path(m["path"])
        source_hash = m["source_hash"]
        label = str(path.relative_to(BUNDLE_ROOT)) if path.is_relative_to(BUNDLE_ROOT) else str(path)

        if result.result.type != "succeeded":
            failed.append((path, str(result.result.type)))
            print(f"  {label}: {result.result.type}")
            continue

        message = result.result.message
        tool_block = next(
            (b for b in message.content if b.type == "tool_use"), None
        )
        if tool_block is None:
            failed.append((path, f"no tool_use (stop_reason={message.stop_reason})"))
            print(f"  {label}: no tool_use")
            continue

        try:
            body = EnrichedBody.model_validate(tool_block.input)
        except ValidationError as e:
            retries.append((result.custom_id, path, source_hash))
            print(f"  {label}: validation failed ({e.error_count()} errors); queued for retry")
            continue

        try:
            fm, _ = parse_concept(path)
        except Exception as e:
            failed.append((path, f"parse: {e}"))
            print(f"  {label}: parse failed: {e}")
            continue

        fm["_source_hash"] = source_hash
        try:
            write_concept(path, fm, assemble_body(body))
            ok += 1
            print(f"  {label}: ok")
        except Exception as e:
            failed.append((path, f"write: {e}"))
            print(f"  {label}: write failed: {e}")

    return ok, retries, failed


def process_entry(
    client: Anthropic,
    entry: dict,
    state: dict,
    allow_retry: bool,
) -> tuple[int, list[tuple[Path, str]]]:
    """Wait → ingest → mark ingested → (optionally) submit one retry batch.

    Returns (ok_count, failures).
    """
    wait_for_batch(client, entry["id"])
    ok, retries, failed = ingest_batch(client, entry)
    entry["ingested"] = True
    save_state(state)

    if retries and allow_retry:
        print(f"\nrebuilding {len(retries)} retry jobs...")
        retry_jobs: list[Job] = []
        for cid, path, source_hash in retries:
            job = rebuild_job_for_retry(cid, path, source_hash)
            if job is None:
                failed.append((path, "could not rebuild for retry"))
                continue
            retry_jobs.append(job)
        if retry_jobs:
            retry_entry = submit_and_track(client, retry_jobs, state, label="retry")
            retry_ok, retry_failed = process_entry(
                client, retry_entry, state, allow_retry=False
            )
            ok += retry_ok
            failed.extend(retry_failed)
    elif retries and not allow_retry:
        # Final-failure path: validation failure on retry.
        for cid, path, _ in retries:
            failed.append((path, "retry: validation failed"))

    return ok, failed


def main() -> int:
    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set in .env", file=sys.stderr)
        return 1

    if not BUNDLE_ROOT.is_dir():
        print(
            f"{BUNDLE_ROOT}/ not found — run build_references.py first",
            file=sys.stderr,
        )
        return 1

    client = Anthropic()
    state = load_state()

    total_ok = 0
    total_failed: list[tuple[Path, str]] = []

    # ─── Phase 1: resume any prior unfingested batches ──────────────────────
    pending_entries = [b for b in state["batches"] if not b.get("ingested")]
    if pending_entries:
        print(f"resuming {len(pending_entries)} unfingested batch(es) from state file")
        for entry in pending_entries:
            print(
                f"\n→ {entry.get('label', 'batch')} {entry['id']} "
                f"(submitted {entry.get('submitted_at', 'unknown')})"
            )
            try:
                ok, failed = process_entry(client, entry, state, allow_retry=True)
                total_ok += ok
                total_failed.extend(failed)
            except Exception as e:
                print(f"  could not process {entry['id']}: {e}", file=sys.stderr)

    # ─── Phase 2: discover concepts that still need enrichment ──────────────
    candidates: list[Path] = []
    for p in sorted(BUNDLE_ROOT.rglob("*.md")):
        if p.name in {"index.md", "log.md"}:
            continue
        try:
            fm, _ = parse_concept(p)
        except Exception:
            continue
        if fm.get("card_eligible"):
            candidates.append(p)

    if not candidates:
        print("no card_eligible concepts found")
        return 0 if not total_failed else 1

    print(f"\ndiscovered {len(candidates)} card-eligible concepts")

    jobs: list[Job] = []
    skip_reasons: dict[str, int] = {}

    for i, path in enumerate(candidates):
        fm, _ = parse_concept(path)
        source_path = pick_source(fm)
        if source_path is None:
            skip_reasons["no parsed source"] = skip_reasons.get("no parsed source", 0) + 1
            continue
        source_text = source_path.read_text()
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if fm.get("_source_hash") == source_hash:
            skip_reasons["source unchanged"] = skip_reasons.get("source unchanged", 0) + 1
            continue
        custom_id = f"c{i:05d}"
        request = build_request(custom_id, fm, source_text, source_path)
        jobs.append(Job(custom_id, path, fm, source_hash, request))

    skip_count = sum(skip_reasons.values())
    for reason, n in skip_reasons.items():
        print(f"skipping {n}: {reason}")

    if not jobs:
        print("nothing new to enrich")
    else:
        # ─── Phase 3: chunked submission + ingest ───────────────────────────
        # Bounds financial exposure per submit (Anthropic batches can't be
        # reliably canceled mid-flight). State-file resume means a Ctrl-C
        # between chunks loses nothing already-paid-for.
        est_cost, est_tokens = estimate_cost(jobs)
        chunks = chunked(jobs, CHUNK_SIZE)
        print(
            f"\nestimate: {len(jobs)} jobs in {len(chunks)} chunk(s) of ≤{CHUNK_SIZE}; "
            f"input ~{est_tokens:,} tokens; "
            f"rough cost ~${est_cost:.2f} (batch-discounted {MODEL})"
        )

        for i, chunk in enumerate(chunks, start=1):
            print(f"\n=== chunk {i}/{len(chunks)} ({len(chunk)} jobs) ===")
            entry = submit_and_track(
                client, chunk, state, label=f"primary-{i}of{len(chunks)}"
            )
            ok, failed = process_entry(client, entry, state, allow_retry=True)
            total_ok += ok
            total_failed.extend(failed)

    print(
        f"\ndone: ok {total_ok}, "
        f"skipped {skip_count if jobs is not None else 0}, "
        f"failed {len(total_failed)}"
    )
    if total_failed:
        print("\nfailures:", file=sys.stderr)
        for p, d in total_failed:
            try:
                rel = p.relative_to(BUNDLE_ROOT)
            except (ValueError, TypeError):
                rel = p
            print(f"  {rel}: {d}", file=sys.stderr)

    return 0 if not total_failed else 1


if __name__ == "__main__":
    sys.exit(main())
