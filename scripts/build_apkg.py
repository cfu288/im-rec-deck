# /// script
# requires-python = ">=3.10"
# dependencies = ["genanki", "pyyaml"]
# ///
"""Build build/im-rec-deck.apkg — a self-contained Anki package containing the
custom GuidelinesCloze notetype + all generated cards.

Reads build/cards.jsonl (one card per line, produced by generate_cards.py) and
looks up each card's concept frontmatter under references/guidelines/ for
Society / Year metadata. No API calls.

GUIDs are reused verbatim from cards.jsonl so reimporting an updated .apkg
overwrites existing notes in place per genanki's same-GUID-same-fields rule.

Deck hierarchy (Anki uses :: as separator, auto-creates missing parents):

    IMRecDeck                              ← parent (study for unified queue)
    ├── Cardiology                         ← per-system auto-created on import
    │   ├── Hypertension (2025 AHA/ACC)    ← one leaf per (topic, year, society)
    │   ├── Hypertension (2017 AHA/ACC)
    │   └── ...
    └── ...

Tags applied to every note (all nested under a single root namespace so they
don't pollute the user's global tag tree):

    im-rec-deck::system::<slug>              im-rec-deck::system::cardiology
    im-rec-deck::topic::<slug>               im-rec-deck::topic::hypertension
    im-rec-deck::society::<slug>             im-rec-deck::society::aha-acc
    im-rec-deck::year::<n>                   im-rec-deck::year::2025              (omitted for living docs)
    im-rec-deck::status::superseded          when this isn't the latest year for its (topic, society)
    im-rec-deck::high-yield                  when manifest flags the topic as high_yield

Usage:
    uv run scripts/build_apkg.py
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional

import genanki
import yaml


def escape_field(s: str) -> str:
    """HTML-escape user content so medical inequalities (<100 bpm, >60 mmHg)
    don't get parsed as malformed HTML tags. Cloze syntax {{cN::...}} uses no
    HTML-special chars so survives escaping unchanged. quote=False keeps " and '
    literal (cleaner in plain-text rendering)."""
    return html.escape(s, quote=False)


INPUT_PATH = Path("build/cards.jsonl")
OUTPUT_PATH = Path("build/im-rec-deck.apkg")
SUBDECK_ROOT = Path("build/decks")
CLASSIFICATIONS_PATH = Path("build/card-classifications.jsonl")
BUNDLE_ROOT = Path("references/guidelines")
MANIFEST_PATH = Path("manifest.yaml")

# Root namespace for ALL tags this script emits — keeps them collapsed under a
# single entry in Anki's tag tree instead of polluting the user's top level
# with multiple flat groups (system::, topic::, year::, etc.).
TAG_ROOT = "im-rec-deck"
HIGH_YIELD_TAG = f"{TAG_ROOT}::high-yield"
DOSING_TAG = f"{TAG_ROOT}::dosing"

# Stable IDs — generated once via random.randrange(1 << 30, 1 << 31).
# Never change these; doing so orphans existing notes in any collection that
# already imported a prior version of this deck.
MODEL_ID = 1_602_734_115
MODEL_NAME = "GuidelinesCloze"

# Parent deck: reuses the original single-deck ID so users who imported the
# pre-hierarchy .apkg get a seamless rename (Anki resolves by ID, applies the
# new name).
PARENT_DECK_ID = 2_059_400_111
PARENT_DECK_NAME = "IMRecDeck"


def _slug(s: str) -> str:
    return (
        (s or "").lower()
        .replace("/", "-")
        .replace(" ", "-")
        .replace(".", "")
        .replace("&", "and")
    )


# Tags that earlier builds emitted as flat top-level entries. We strip them
# from cards.jsonl before re-emitting under the TAG_ROOT namespace so the tag
# tree doesn't keep two parallel hierarchies. Note that re-importing the .apkg
# only WRITES our new tag set; Anki won't remove flat tags that older imports
# already attached to existing notes. Use the cleanup recipe in the script's
# top docstring or the Browse → bulk-remove-tag UI to scrub those.
_SUPPLANTED_PREFIXES = (
    "system::", "topic::", "society::", "year::", "status::", "guidelines::",
)
_SUPPLANTED_EXACT = {"guidelines", "high-yield"}


def _is_supplanted_tag(t: str) -> bool:
    if t in _SUPPLANTED_EXACT:
        return True
    return any(t.startswith(p) for p in _SUPPLANTED_PREFIXES)


def deck_id_for(name: str) -> int:
    """Deterministic positive 63-bit int for a deck name.

    Anki deck IDs must be unique within a collection but are otherwise opaque.
    Hashing the name means the same deck always gets the same ID across
    rebuilds, so re-imports update in place without orphaning the deck tree.
    """
    if name == PARENT_DECK_NAME:
        return PARENT_DECK_ID
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF or 1


CARD_TEMPLATE_FRONT = "{{cloze:Text}}"

CARD_TEMPLATE_BACK = """{{cloze:Text}}

{{#Back Extra}}<div class="back-extra">{{Back Extra}}</div>{{/Back Extra}}

<hr>
<div class="meta">
  <div class="source"><b>Source:</b> {{Source}}</div>
  <div class="provenance">
    {{Society}}{{#Year}} &middot; {{Year}}{{/Year}} &middot; <span class="path">{{System}} / {{Topic}}</span>
  </div>
  {{#Site}}<div class="site-link"><a href="{{Site}}">Read the summary &amp; open the guideline &rarr;</a></div>{{/Site}}
</div>
"""

CARD_CSS = """\
.card {
  font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
  font-size: 20px;
  text-align: left;
  color: #222;
  background: #fafafa;
  padding: 1em;
  line-height: 1.5;
}
.cloze { color: #d33; font-weight: 600; }
.back-extra { margin: 0.75em 0; color: #444; }
hr { border: 0; border-top: 1px solid #ddd; margin: 1em 0 0.5em; }
.meta { font-size: 0.78em; color: #777; line-height: 1.4; }
.source { margin-bottom: 0.25em; }
.provenance .path { font-family: ui-monospace, SFMono-Regular, monospace; color: #999; }
.site-link { margin-top: 0.5em; }
.site-link a { color: #3182ce; text-decoration: none; }
.site-link a:hover { text-decoration: underline; }
"""


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


class Recipe(NamedTuple):
    guid: str
    deck_path: str
    system_title_deck: str  # "IMRecDeck::Cardiology"
    concept_key: tuple[str, str, str]  # (system_slug, topic_slug, version_slug)
    fields: list[str]
    tags: list[str]


def load_concept_frontmatter(system: str, topic: str, version: str) -> dict:
    path = BUNDLE_ROOT / system / topic / f"{version}.md"
    if not path.is_file():
        return {}
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


def load_manifest_context() -> dict:
    """Load manifest-derived state used to build deck names + tags:

        system_titles    {system_slug: display title}
        topic_titles     {(system_slug, topic_slug): display title}
        high_yield_topics {(system_slug, topic_slug)}
        max_year_for     {(system_slug, topic_slug, society): max_year}
                         used to compute the status::superseded tag
    """
    out = {
        "system_titles": {},
        "topic_titles": {},
        "high_yield_topics": set(),
        "max_year_for": {},
    }
    if not MANIFEST_PATH.is_file():
        return out
    data = yaml.safe_load(MANIFEST_PATH.read_text()) or {}
    for sys_slug, system in (data.get("systems") or {}).items():
        out["system_titles"][sys_slug] = system.get("title", sys_slug)
        topic_society_default = None  # not used; topic-level society overrides per-version
        for topic_slug, topic in (system.get("topics") or {}).items():
            out["topic_titles"][(sys_slug, topic_slug)] = topic.get("title", topic_slug)
            if topic.get("high_yield"):
                out["high_yield_topics"].add((sys_slug, topic_slug))
            topic_society = topic.get("society")
            for v in topic.get("versions") or []:
                y = v.get("year")
                if y is None:
                    continue
                soc = v.get("society") or topic_society
                key = (sys_slug, topic_slug, soc)
                out["max_year_for"][key] = max(out["max_year_for"].get(key, y), y)
    return out


def deck_name_for(system: str, topic: str, society: str, year: Optional[int],
                  ctx: dict) -> str:
    """Build the full leaf deck path, e.g.
        IMRecDeck::Cardiology::Hypertension (2025 AHA/ACC)
    """
    sys_title = ctx["system_titles"].get(system, system)
    topic_title = ctx["topic_titles"].get((system, topic), topic)
    if year is not None and society:
        leaf = f"{topic_title} ({year} {society})"
    elif year is not None:
        leaf = f"{topic_title} ({year})"
    elif society:
        leaf = f"{topic_title} ({society})"
    else:
        leaf = topic_title
    return f"{PARENT_DECK_NAME}::{sys_title}::{leaf}"


def build_model() -> genanki.Model:
    return genanki.Model(
        MODEL_ID,
        MODEL_NAME,
        fields=[
            {"name": "Text"},
            {"name": "Back Extra"},
            {"name": "Source"},
            {"name": "System"},
            {"name": "Topic"},
            {"name": "Society"},
            {"name": "Year"},
            # Appended field — safe on re-import because MODEL_ID is unchanged
            # and Anki upgrades notes in place, populating existing rows with
            # the new field's value on next import.
            {"name": "Site"},
        ],
        templates=[
            {
                "name": "Cloze",
                "qfmt": CARD_TEMPLATE_FRONT,
                "afmt": CARD_TEMPLATE_BACK,
            }
        ],
        css=CARD_CSS,
        model_type=genanki.Model.CLOZE,
        sort_field_index=2,  # Source — Text is noisy because of {{c1::...}} markup
    )


def main() -> int:
    if not INPUT_PATH.is_file():
        print(
            f"{INPUT_PATH} not found — run generate_cards.py first",
            file=sys.stderr,
        )
        return 1

    model = build_model()
    ctx = load_manifest_context()
    print(
        f"manifest: {len(ctx['system_titles'])} systems · "
        f"{len(ctx['topic_titles'])} topics · "
        f"{len(ctx['high_yield_topics'])} high-yield"
    )

    # Load per-card dosing classifications if available (optional — produced by
    # scripts/classify_dosing.py). Used to add the im-rec-deck::dosing tag.
    dosing_guids: set[str] = set()
    if CLASSIFICATIONS_PATH.is_file():
        with CLASSIFICATIONS_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("is_dosing") and r.get("guid"):
                    dosing_guids.add(r["guid"])
        print(f"classifications: {len(dosing_guids)} cards flagged as dosing")
    else:
        print(
            f"classifications: {CLASSIFICATIONS_PATH} not found — "
            "dosing tag will not be applied (run `just qa-dosing` to generate)"
        )

    fm_cache: dict[tuple[str, str, str], dict] = {}

    def get_fm(system: str, topic: str, version: str) -> dict:
        key = (system, topic, version)
        if key not in fm_cache:
            fm_cache[key] = load_concept_frontmatter(system, topic, version)
        return fm_cache[key]

    # Phase 1: build note recipes so we can assemble the mega deck AND per-concept
    # sub-decks from the same source-of-truth records. genanki.Note instances are
    # bound to a single Deck, so we create fresh Notes in each output phase.
    recipes: list[Recipe] = []
    n_rows = 0
    seen_guids: set[str] = set()
    deck_card_counts: dict[str, int] = {}

    with INPUT_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            n_rows += 1
            row = json.loads(line)

            guid = row["guid"]
            if guid in seen_guids:
                continue
            seen_guids.add(guid)

            meta = row.get("_meta") or {}
            system = meta.get("system", "")
            topic = meta.get("topic", "")
            version = meta.get("version", "")

            fm = get_fm(system, topic, version) if system and topic and version else {}
            society = str(fm.get("society", "") or "")
            year_val: Optional[int] = fm.get("year")
            year = str(year_val) if year_val is not None else ""

            deck_path = deck_name_for(system, topic, society, year_val, ctx)
            deck_card_counts[deck_path] = deck_card_counts.get(deck_path, 0) + 1

            sys_title = ctx["system_titles"].get(system, system)
            system_title_deck = f"{PARENT_DECK_NAME}::{sys_title}"

            raw_tags = (row.get("tags") or "").split()
            tags = [t for t in raw_tags if not _is_supplanted_tag(t)]
            tag_set = set(tags)

            def add_tag(t: str):
                if t and t not in tag_set:
                    tags.append(t)
                    tag_set.add(t)

            if system:
                add_tag(f"{TAG_ROOT}::system::{_slug(system)}")
            if topic:
                add_tag(f"{TAG_ROOT}::topic::{_slug(topic)}")
            if society:
                add_tag(f"{TAG_ROOT}::society::{_slug(society)}")
            if year_val is not None:
                add_tag(f"{TAG_ROOT}::year::{year_val}")
            if (system, topic) in ctx["high_yield_topics"]:
                add_tag(HIGH_YIELD_TAG)
            if guid in dosing_guids:
                add_tag(DOSING_TAG)
            max_year = ctx["max_year_for"].get((system, topic, society))
            if year_val is not None and max_year is not None and year_val < max_year:
                add_tag(f"{TAG_ROOT}::status::superseded")

            # Deep-dive page URL for this card's concept. Kept as an absolute
            # URL (not relative) so the link works when a card is reviewed
            # inside Anki — the client renders HTML but has no baseurl context.
            site_url = (
                f"https://cfu288.github.io/im-rec-deck/"
                f"{system}/{topic}/{version}/"
                if system and topic and version
                else ""
            )

            recipes.append(Recipe(
                guid=guid,
                deck_path=deck_path,
                system_title_deck=system_title_deck,
                concept_key=(system, topic, version),
                fields=[
                    escape_field(row.get("text", "")),
                    "",  # Back Extra — empty for now
                    escape_field(row.get("extra", "")),
                    system,
                    topic,
                    escape_field(society),
                    year,
                    site_url,
                ],
                tags=tags,
            ))

    def make_note(r: Recipe) -> genanki.Note:
        return genanki.Note(model=model, fields=r.fields, tags=r.tags, guid=r.guid)

    # Phase 2a: mega deck — everything under one .apkg
    mega_parent = genanki.Deck(PARENT_DECK_ID, PARENT_DECK_NAME)
    mega_decks: dict[str, genanki.Deck] = {PARENT_DECK_NAME: mega_parent}
    for r in recipes:
        if r.system_title_deck not in mega_decks:
            mega_decks[r.system_title_deck] = genanki.Deck(
                deck_id_for(r.system_title_deck), r.system_title_deck
            )
        if r.deck_path not in mega_decks:
            mega_decks[r.deck_path] = genanki.Deck(deck_id_for(r.deck_path), r.deck_path)
        mega_decks[r.deck_path].add_note(make_note(r))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    genanki.Package(list(mega_decks.values())).write_to_file(str(OUTPUT_PATH))

    # Phase 2b: per-concept sub-decks — one .apkg per (system, topic, version).
    # Each carries the same stable IDs for parent + system + leaf so importing
    # multiple sub-decks (or a sub-deck then the mega) merges cleanly on GUID
    # for notes and on deck-ID for the hierarchy — no orphan trees, no dupe
    # notes, FSRS history preserved.
    by_concept: dict[tuple[str, str, str], list[Recipe]] = {}
    for r in recipes:
        by_concept.setdefault(r.concept_key, []).append(r)

    n_subdecks = 0
    for (system, topic, version), group in by_concept.items():
        if not (system and topic and version):
            continue
        sub_parent = genanki.Deck(PARENT_DECK_ID, PARENT_DECK_NAME)
        sub_system = genanki.Deck(
            deck_id_for(group[0].system_title_deck), group[0].system_title_deck
        )
        sub_leaf = genanki.Deck(deck_id_for(group[0].deck_path), group[0].deck_path)
        for r in group:
            sub_leaf.add_note(make_note(r))
        out = SUBDECK_ROOT / system / topic / f"{version}.apkg"
        out.parent.mkdir(parents=True, exist_ok=True)
        genanki.Package([sub_parent, sub_system, sub_leaf]).write_to_file(str(out))
        n_subdecks += 1

    n_added = len(recipes)
    dup_skipped = n_rows - n_added
    n_leaf_decks = len(mega_decks) - 1  # minus parent (system intermediates counted)
    print(f"wrote {n_added} notes → {OUTPUT_PATH}")
    if dup_skipped:
        print(f"  (skipped {dup_skipped} rows with duplicate GUIDs)")
    print(f"  model: {MODEL_NAME} (id={MODEL_ID}) · 8 fields · cloze")
    print(f"  parent deck: {PARENT_DECK_NAME!r} (id={PARENT_DECK_ID})")
    print(f"  leaf decks:  {n_leaf_decks}")
    print(f"  per-concept sub-decks: {n_subdecks} → {SUBDECK_ROOT}/<sys>/<topic>/<version>.apkg")
    if deck_card_counts:
        top = sorted(deck_card_counts.items(), key=lambda kv: -kv[1])[:5]
        print(f"  largest decks:")
        for name, n in top:
            short = name.split("::", 1)[-1] if "::" in name else name
            print(f"    {n:4d}  {short}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
