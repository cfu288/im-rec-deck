# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Repo self-test: catches the failure classes that cost correction rounds.

Each check exists because its absence bit us once (see AGENTS.md incident log):

  idempotency   — generators run twice must be byte-identical (formatter drift,
                  .rstrip blank-line bug busted body_hash idempotency)
  preservation  — build_references must never reduce the enriched-body count
                  (the June 2026 rmtree wipe destroyed 96 paid enrichments)
  links         — every .apkg linked from docs/ exists on disk, and every
                  sub-deck on disk is linked from docs/ (orphaned CHEST deck)
  coverage      — every manifest topic resolves a current version (7 living
                  guidelines were once silently excluded); every enriched
                  card_eligible concept has cards in cards.jsonl
  jargon        — site copy is for med students/residents; no GUID/FSRS/
                  canonical (each was shipped once and corrected by the user)
  apkg          — the built deck actually contains the cards (a TSV once
                  failed 653/653 on Anki import despite being "well-formed")

Free and read-mostly: runs the two free generators (in place — they are
idempotent, which is exactly what's being verified) and reads everything else.

Usage:
    uv run scripts/check_repo.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml

from validate_manifest import Manifest
from build_references import current_version, version_slug

REPO = Path(__file__).resolve().parent.parent
REFERENCES = REPO / "references" / "guidelines"
DOCS = REPO / "docs"
BUILD = REPO / "build"
CARDS = BUILD / "cards.jsonl"
MEGA_APKG = BUILD / "im-rec-deck.apkg"
DECKS = BUILD / "decks"

ENRICHED_MARKER = "# Key Recommendations"

failures: list[str] = []


def fail(check: str, msg: str) -> None:
    failures.append(f"[{check}] {msg}")
    print(f"  FAIL {msg}")


def ok(msg: str) -> None:
    print(f"  ok   {msg}")


def snapshot(*roots: Path) -> dict[str, str]:
    """path -> sha256 of every file under the given roots."""
    out: dict[str, str] = {}
    for root in roots:
        for p in sorted(root.rglob("*")):
            if p.is_file():
                out[str(p.relative_to(REPO))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def run_generator(script: str) -> None:
    r = subprocess.run(
        ["uv", "run", f"scripts/{script}"], cwd=REPO, capture_output=True, text=True
    )
    if r.returncode != 0:
        fail("idempotency", f"{script} exited {r.returncode}: {r.stderr.strip()[:300]}")


def count_enriched() -> int:
    return sum(
        1
        for p in REFERENCES.rglob("*.md")
        if p.name != "index.md" and ENRICHED_MARKER in p.read_text()
    )


def check_idempotency_and_preservation() -> None:
    print("\n== idempotency + preservation (run generators twice, compare) ==")
    enriched_before = count_enriched()

    run_generator("build_references.py")
    run_generator("build_docs.py")
    snap1 = snapshot(REFERENCES, DOCS)

    run_generator("build_references.py")
    run_generator("build_docs.py")
    snap2 = snapshot(REFERENCES, DOCS)

    drifted = sorted(
        set(k for k in snap1 if snap1.get(k) != snap2.get(k))
        | (set(snap1) ^ set(snap2))
    )
    if drifted:
        for f in drifted[:10]:
            fail("idempotency", f"second run changed {f}")
        if len(drifted) > 10:
            fail("idempotency", f"...and {len(drifted) - 10} more")
    else:
        ok(f"generators are idempotent across {len(snap1)} files")

    enriched_after = count_enriched()
    if enriched_after < enriched_before:
        fail(
            "preservation",
            f"enriched bodies decreased {enriched_before} -> {enriched_after} — "
            "a generator clobbered paid enrichment work",
        )
    else:
        ok(f"enriched bodies preserved ({enriched_after})")


APKG_LINK_RE = re.compile(r"raw/main/(build/[^)\"'\s]+?\.apkg)")


def check_links() -> None:
    print("\n== docs <-> deck link integrity ==")
    linked: set[str] = set()
    for p in DOCS.rglob("*.md"):
        for m in APKG_LINK_RE.finditer(p.read_text()):
            rel = m.group(1)
            linked.add(rel)
            if not (REPO / rel).is_file():
                fail("links", f"{p.relative_to(REPO)} links missing file {rel}")
    on_disk = {str(p.relative_to(REPO)) for p in DECKS.rglob("*.apkg")}
    orphans = on_disk - linked
    for o in sorted(orphans):
        fail("links", f"orphan sub-deck not linked from docs/: {o}")
    if not failures or not any(f.startswith("[links]") for f in failures):
        ok(f"{len(linked)} deck links all resolve; no orphan sub-decks")


def check_coverage() -> None:
    print("\n== manifest coverage ==")
    manifest = Manifest.model_validate(yaml.safe_load((REPO / "manifest.yaml").read_text()))

    carded: set[tuple[str, str, str]] = set()
    if CARDS.is_file():
        with CARDS.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                meta = json.loads(line).get("_meta") or {}
                carded.add((meta.get("system"), meta.get("topic"), meta.get("version")))

    missing_current = 0
    missing_cards = 0
    for sys_slug, system in manifest.systems.items():
        for topic_slug, topic in system.topics.items():
            cur = current_version(topic)
            if cur is None:
                fail("coverage", f"{sys_slug}/{topic_slug}: no current version resolvable")
                missing_current += 1
                continue
            vslug = version_slug(cur, topic.society)
            ref = REFERENCES / sys_slug / topic_slug / f"{vslug}.md"
            if not ref.is_file():
                continue  # skeleton not built yet; build_references failure would catch
            fm_text = ref.read_text()
            enriched = ENRICHED_MARKER in fm_text
            if enriched and (sys_slug, topic_slug, vslug) not in carded:
                fail(
                    "coverage",
                    f"{sys_slug}/{topic_slug}/{vslug}: enriched + card_eligible but has no cards",
                )
                missing_cards += 1
    if not missing_current and not missing_cards:
        ok("every topic resolves a current version; every enriched current version has cards")


BANNED = [
    (re.compile(r"\bGUID\b"), "GUID"),
    (re.compile(r"\bFSRS\b"), "FSRS"),
    (re.compile(r"\bcanonical\b", re.I), "canonical"),
]


def check_jargon() -> None:
    print("\n== site-copy jargon lint (audience: med students/residents) ==")
    hits = 0
    for p in sorted(DOCS.rglob("*.md")):
        text = p.read_text()
        for pat, word in BANNED:
            for _ in pat.finditer(text):
                fail("jargon", f"{p.relative_to(REPO)}: banned term '{word}'")
                hits += 1
    if not hits:
        ok("no banned developer jargon in docs/")


def check_apkg() -> None:
    print("\n== apkg import smoke test ==")
    if not MEGA_APKG.is_file():
        fail("apkg", f"{MEGA_APKG.relative_to(REPO)} missing")
        return
    expected = set()
    with CARDS.open() as f:
        for line in f:
            line = line.strip()
            if line:
                expected.add(json.loads(line)["guid"])

    with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(MEGA_APKG) as z:
        names = z.namelist()
        db_name = next((n for n in ("collection.anki21", "collection.anki2") if n in names), None)
        if db_name is None:
            fail("apkg", f"no collection db inside apkg (contents: {names[:5]})")
            return
        z.extract(db_name, td)
        con = sqlite3.connect(Path(td) / db_name)
        try:
            (n_notes,) = con.execute("select count(*) from notes").fetchone()
            guids = {row[0] for row in con.execute("select guid from notes")}
        finally:
            con.close()

    if n_notes != len(expected):
        fail("apkg", f"deck has {n_notes} notes, cards.jsonl has {len(expected)}")
    elif guids != expected:
        fail("apkg", f"note GUIDs diverge from cards.jsonl ({len(guids ^ expected)} mismatched)")
    else:
        ok(f"mega deck imports cleanly with all {n_notes} notes, GUIDs match cards.jsonl")


def main() -> int:
    check_idempotency_and_preservation()
    check_links()
    check_coverage()
    check_jargon()
    check_apkg()

    print()
    if failures:
        print(f"CHECK FAILED — {len(failures)} problem(s):", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("CHECK OK — all repo self-tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
