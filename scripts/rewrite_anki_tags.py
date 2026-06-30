# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx", "pyyaml"]
# ///
"""Rewrite Anki tags in-place via AnkiConnect, driven by a YAML mapping.

Default is DRY-RUN — prints exactly what would change without mutating
anything. Pass --apply to actually do it. Always run dry-run first.

Requires Anki desktop running with the AnkiConnect add-on installed
(https://ankiweb.net/shared/info/2055492159, listens on localhost:8765).

The mapping YAML has two keys:
    mapping:  {old_tag: new_tag}  OR  {old_tag: [new_tag1, new_tag2, ...]}
    drop:     [old_tag, ...]      tags to remove with no replacement

For one-to-many entries (mapping value is a list), each new tag is added in
parallel; the old tag is removed. This is the right behavior when an old tag
straddled two axes (e.g. `Bisphosphonates::osteoporosis` should become both
`tx::bisphosphonate` and `dx::osteoporosis`).

Usage:
    # Dry-run, scoped to the Rheumatology deck:
    uv run scripts/rewrite_anki_tags.py spec/rheumatology-tag-mapping.yaml

    # Apply for real (after reviewing dry-run output):
    uv run scripts/rewrite_anki_tags.py spec/rheumatology-tag-mapping.yaml --apply

    # Override the deck filter (default: Rheumatology):
    uv run scripts/rewrite_anki_tags.py mapping.yaml --deck "My Other Deck"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml


ANKICONNECT_URL = "http://localhost:8765"


def ac(action: str, **params: Any) -> Any:
    """Call AnkiConnect. Raises on transport error or AnkiConnect error."""
    resp = httpx.post(
        ANKICONNECT_URL,
        json={"action": action, "version": 6, "params": params},
        timeout=60.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"{action}: {body['error']}")
    return body["result"]


def normalize_new(value: Any) -> list[str]:
    """Coerce a mapping value to a list of new tags."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(f"unexpected mapping value: {value!r}")


def quote_for_anki(s: str) -> str:
    """Quote a query token for AnkiConnect's findNotes; backslash-escape any
    inner quote (rare in tags, but safe)."""
    return s.replace('"', r"\"")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mapping_yaml", type=Path, help="path to the mapping YAML")
    ap.add_argument(
        "--deck",
        default="Rheumatology",
        help="restrict to this deck (default: Rheumatology)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually mutate (default: dry-run prints changes only)",
    )
    args = ap.parse_args()

    if not args.mapping_yaml.is_file():
        print(f"mapping file not found: {args.mapping_yaml}", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(args.mapping_yaml.read_text()) or {}
    mapping = cfg.get("mapping") or {}
    drops = cfg.get("drop") or []

    try:
        version = ac("version")
    except Exception as e:
        print(
            f"AnkiConnect not reachable at {ANKICONNECT_URL}: {e}\n"
            "Is Anki running, with the AnkiConnect add-on installed?",
            file=sys.stderr,
        )
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    deck_q = f'"deck:{quote_for_anki(args.deck)}"'
    print(
        f"[{mode}] AnkiConnect v{version}, deck filter: {args.deck!r}\n"
        f"  {len(mapping)} renames, {len(drops)} drops queued\n"
    )

    counts = {"renamed": 0, "dropped": 0, "no_matches": 0, "notes_touched": set()}

    # ── Renames ────────────────────────────────────────────────────────
    for old_tag, raw_new in mapping.items():
        new_tags = normalize_new(raw_new)
        new_str = " ".join(new_tags)
        old_quoted = quote_for_anki(old_tag)

        query = f'{deck_q} "tag:{old_quoted}"'
        ids = ac("findNotes", query=query)
        if not ids:
            counts["no_matches"] += 1
            continue

        # One-to-many or one-to-one
        arrow = "→"
        if len(new_tags) > 1:
            arrow = "→×%d→" % len(new_tags)
        print(f"  {old_tag!r:<45} {arrow}  {new_str!r:<55}  ({len(ids):>3} notes)")

        if args.apply:
            if new_str:
                ac("addTags", notes=ids, tags=new_str)
            ac("removeTags", notes=ids, tags=old_tag)

        counts["renamed"] += 1
        counts["notes_touched"].update(ids)

    # ── Drops (no replacement) ─────────────────────────────────────────
    if drops:
        print("\n--- drops ---")
    for old_tag in drops:
        old_quoted = quote_for_anki(old_tag)
        ids = ac("findNotes", query=f'{deck_q} "tag:{old_quoted}"')
        if not ids:
            counts["no_matches"] += 1
            continue
        print(f"  DROP {old_tag!r:<55}  ({len(ids):>3} notes)")
        if args.apply:
            ac("removeTags", notes=ids, tags=old_tag)
        counts["dropped"] += 1
        counts["notes_touched"].update(ids)

    # ── Summary ────────────────────────────────────────────────────────
    print(
        f"\n[{mode}] summary: "
        f"renamed {counts['renamed']}, "
        f"dropped {counts['dropped']}, "
        f"no-match {counts['no_matches']}, "
        f"unique notes touched {len(counts['notes_touched'])}"
    )
    if not args.apply:
        print("(dry-run — rerun with --apply to mutate)")
    else:
        # Clear empty tags from the collection (AnkiConnect housekeeping)
        try:
            ac("clearUnusedTags")
            print("cleared unused tags from collection")
        except Exception as e:
            print(f"warning: clearUnusedTags failed: {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
