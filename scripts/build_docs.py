# /// script
# requires-python = ">=3.10"
# dependencies = ["ruamel.yaml"]
# ///
"""Generate Jekyll-rendered listing pages under docs/ from manifest.yaml.

Layout:
    docs/index.md          — root: list of systems with topic count each
    docs/<system>.md       — per system: topics with all versions inline

Source-file local paths are intentionally NOT linked from these pages — they
are copyrighted and gitignored. Only public publisher URLs are surfaced.
"""

from __future__ import annotations

import re
from pathlib import Path
from ruamel.yaml import YAML

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "manifest.yaml"
DOCS = REPO / "docs"
REFERENCES = REPO / "references" / "guidelines"

yaml = YAML()

SKELETON_BODY_MARKER = "_Body pending Stage 2 enrichment from parsed source._"

# Inline Anki icon used next to every download link. Height matches text so it
# reads as a bullet marker, not a graphic. alt="" because the link text is
# already descriptive — screen readers should skip the img. Uses Liquid inline
# so Jekyll resolves baseurl at render time (works under the /im-rec-deck/
# project-page path AND under root-domain hosting without changes).
ANKI_ICON = (
    "<img src=\"{{ '/assets/anki.png' | relative_url }}\" alt=\"\" "
    "style=\"height:1.1em;vertical-align:-0.2em;margin-right:0.25em\">"
)
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
SUMMARY_SECTION_RE = re.compile(
    r"^#\s+Summary\s*\n+(.*?)(?=\n#\s|\Z)", re.MULTILINE | re.DOTALL
)


def slugify_society(s):
    if not s:
        return "unknown"
    s = s.lower()
    s = re.sub(r"\s*&\s*", "-and-", s)
    s = re.sub(r"[/\s]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    return re.sub(r"-+", "-", s).strip("-")


def reference_summary(
    system_slug: str,
    topic_slug: str,
    versions: list[dict],
    topic_society: str | None,
) -> str | None:
    """Pull the `# Summary` paragraph from the current version's reference file.

    Mirrors `scripts/build_references.py:version_slug` for the filename, which is
    `<year>-<society>` if a year is present, else just `<society>`. Society
    inherits from topic when the version doesn't carry its own (most versions in
    this manifest don't). Picks the latest-year version as "current", matching
    `build_references.py:current_version`.

    Returns None when:
      - no dated version exists,
      - the reference file is missing,
      - the body is still the unenriched skeleton (so the docs site doesn't
        show "_Body pending..._" to readers).
    """
    dated = [v for v in versions if v.get("year") is not None]
    if dated:
        current = max(dated, key=lambda v: v["year"])
    elif versions:
        # Yearless live guidelines (HCV, CDC/ACIP, etc.) — first listed wins.
        # Mirrors build_references.py:current_version logic.
        current = versions[0]
    else:
        return None
    society = current.get("society") or topic_society
    society_slug = slugify_society(society) if society else "unknown"
    slug = (
        f"{current['year']}-{society_slug}"
        if current.get("year") is not None
        else society_slug
    )
    path = REFERENCES / system_slug / topic_slug / f"{slug}.md"
    if not path.is_file():
        return None
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    body = m.group(2) if m else text
    if SKELETON_BODY_MARKER in body:
        return None
    sm = SUMMARY_SECTION_RE.search(body)
    if not sm:
        return None
    summary = sm.group(1).strip()
    return summary or None


def fmt_year_society(year, society):
    if year and society:
        return f"{year} · {society}"
    if year:
        return str(year)
    return society or "(no year)"


def site_url(path: str) -> str:
    """Emit a Liquid expression that resolves to a baseurl-aware site URL.

    Jekyll processes Liquid before kramdown, so `[label]({{ '/x/' | relative_url }})`
    is rewritten to the correct href whether baseurl is `/im-rec-deck`
    or empty. Works on GH Pages project sites AND root-domain hosting.

    NOT suitable for `permalink:` frontmatter — Jekyll doesn't expand Liquid
    there; use `system_permalink()` for that.
    """
    return "{{ '" + path + "' | relative_url }}"


def system_permalink(slug: str) -> str:
    """Bare path for `permalink:` frontmatter. Jekyll prepends baseurl automatically
    for permalinks at render time."""
    return f"/{slug}/"


def system_link(slug: str) -> str:
    """Liquid-wrapped href for use inside markdown links."""
    return site_url(f"/{slug}/")


def version_slug(v: dict, topic_society: str | None) -> str:
    """Filename slug for a version. Mirrors scripts/build_references.py:version_slug
    so docs deep-dive URLs align with references/ filenames."""
    society = v.get("society") or topic_society
    soc_part = slugify_society(society) if society else "unknown"
    year = v.get("year")
    return f"{year}-{soc_part}" if year is not None else soc_part


def render_version(
    v: dict,
    topic_society: str | None,
    system_slug: str | None = None,
    topic_slug: str | None = None,
) -> list[str]:
    out: list[str] = []
    year = v.get("year")
    society = v.get("society") or topic_society
    title = v.get("title") or "(no title)"
    # Wrap the title in a link to the version's deep-dive page when we have
    # an enriched body on disk for it (i.e. an enrichment run has produced
    # the Key Recommendations / Thresholds & Doses / Citations sections).
    # Non-enriched versions render as plain text — no dead links.
    link_title = title
    is_enriched = False
    is_card_eligible = False
    vslug = None
    if system_slug and topic_slug:
        vslug = version_slug(v, topic_society)
        ref_path = REFERENCES / system_slug / topic_slug / f"{vslug}.md"
        if ref_path.is_file():
            body = ref_path.read_text()
            m = FRONTMATTER_RE.match(body)
            body_only = m.group(2) if m else body
            # Only card_eligible versions get a sub-deck from build_apkg.py; a
            # superseded version keeps its enriched body but its cards are
            # retired, so it must not advertise a (now-orphaned) .apkg.
            is_card_eligible = bool(m) and "card_eligible: true" in m.group(1)
            if (
                SKELETON_BODY_MARKER not in body_only
                and "# Key Recommendations" in body_only
            ):
                is_enriched = True
                href = site_url(f"/{system_slug}/{topic_slug}/{vslug}/")
                link_title = f"[{title}]({href})"
    out.append(f"  - **{fmt_year_society(year, society)}** — {link_title}")

    # needs_attention is a maintainer-only signal (surfaced in
    # spec/manifest-needs-attention.md); not rendered on the public site.

    notes = v.get("notes")
    if notes:
        out.append(f"    - _{notes}_")

    src = v.get("source") or {}
    # Publisher URLs, labeled in reader language (no "canonical" — that's a
    # web-metadata term). v['url'] (usually the DOI landing page) collapses
    # into the "html" slot when no format-specific html URL is on file;
    # otherwise the format-specific one wins because it's usually the full-text
    # version instead of a stub.
    format_urls: dict[str, str] = {}
    if isinstance(src, dict):
        for fmt in ("html", "pdf", "epub", "pmc", "xml"):
            entry = src.get(fmt)
            if isinstance(entry, dict) and entry.get("url"):
                format_urls[fmt] = entry["url"]
    if v.get("url") and "html" not in format_urls:
        format_urls["html"] = v["url"]

    source_links: list[str] = [
        f"[{fmt}]({url})" for fmt, url in format_urls.items()
    ]
    if v.get("pmid"):
        source_links.append(
            f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{v['pmid']}/)"
        )

    row_parts: list[str] = []
    if source_links:
        row_parts.append("Read the guideline: " + " · ".join(source_links))
    # Anki sub-deck download alongside the source links — only when the version
    # is the current, card_eligible one (which is what build_apkg.py emits a
    # matching .apkg for). Superseded versions have no deck.
    if is_enriched and is_card_eligible and vslug and system_slug and topic_slug:
        subdeck_url = (
            "https://github.com/cfu288/im-rec-deck/raw/main/build/decks/"
            f"{system_slug}/{topic_slug}/{vslug}.apkg"
        )
        row_parts.append(f"[{ANKI_ICON}Anki deck]({subdeck_url})")
    if row_parts:
        out.append(f"    - {' · '.join(row_parts)}")
    return out


def render_topic(system_slug: str, topic_slug: str, topic_block: dict) -> list[str]:
    out: list[str] = []
    title = topic_block.get("title") or topic_slug
    society = topic_block.get("society")
    high_yield = topic_block.get("high_yield")
    header = f"### {title}"
    if high_yield:
        header += " ⭐"
    out.append(header)
    out.append("")
    meta_parts = [f"`{topic_slug}`"]
    if society:
        meta_parts.append(f"society: **{society}**")
    if high_yield:
        meta_parts.append("**high-yield**")
    out.append(" · ".join(meta_parts))
    out.append("")
    summary = reference_summary(
        system_slug, topic_slug, topic_block.get("versions") or [], society
    )
    if summary:
        out.append(summary)
        out.append("")
    for v in topic_block.get("versions", []):
        out.extend(render_version(v, society, system_slug, topic_slug))
    out.append("")
    return out


SYSTEM_NAV_ORDER: dict[str, int] = {}


def render_system(system_slug: str, sys_block: dict) -> str:
    title = sys_block.get("title", system_slug)
    # nav_order for just-the-docs sidebar. Assigned in first-seen order from the
    # caller, so main() controls the ordering (alphabetical by slug currently).
    nav_order = SYSTEM_NAV_ORDER.get(system_slug, 99)
    lines: list[str] = ["---"]
    lines.append(f"title: {title!r}")
    lines.append(f"permalink: {system_permalink(system_slug)}")
    lines.append(f"nav_order: {nav_order}")
    lines.append("has_children: true")
    lines.append("---")
    lines.append("")
    if sys_block.get("description"):
        lines.append(f"> {sys_block['description']}")
        lines.append("")
    topics = sys_block.get("topics") or {}
    n_topics = len(topics)
    n_versions = sum(len(t.get("versions", [])) for t in topics.values())
    n_hy = sum(1 for t in topics.values() if t.get("high_yield"))
    lines.append(
        f"**{n_topics} topics** · **{n_versions} versions** · **{n_hy} high-yield**"
    )
    lines.append("")
    lines.append(f"[← back to all systems]({site_url('/')})")
    lines.append("")
    for topic_slug in sorted(topics):
        lines.extend(render_topic(system_slug, topic_slug, topics[topic_slug]))
    return "\n".join(lines) + "\n"


def render_index(manifest: dict) -> str:
    # just-the-docs: title + nav_order place this at the top of the sidebar.
    lines = ["---", "title: Home", "nav_order: 1", "---", ""]
    lines.append(
        f"# {manifest.get('title', 'IMRecDeck') if isinstance(manifest, dict) else 'IMRecDeck'}"
    )
    lines.append("")
    lines.append(
        "System-by-system index of current society guidelines for internal medicine "
        "practice. Click into a system to browse its "
        "topics and per-society / per-year versions, with links to publisher pages."
    )
    lines.append("")
    lines.append("⭐ marks topics flagged as high-yield.")
    lines.append("")
    lines.append("## Systems")
    lines.append("")
    systems = manifest.get("systems") or {}
    total_topics = total_versions = total_hy = 0
    for system_slug in sorted(systems):
        sb = systems[system_slug]
        topics = sb.get("topics") or {}
        n_topics = len(topics)
        n_versions = sum(len(t.get("versions", [])) for t in topics.values())
        n_hy = sum(1 for t in topics.values() if t.get("high_yield"))
        total_topics += n_topics
        total_versions += n_versions
        total_hy += n_hy
        desc = sb.get("description") or ""
        title = sb.get("title", system_slug)
        lines.append(
            f"- [{title}]({system_link(system_slug)}) — {desc} ({n_topics} topics, {n_hy} ⭐)"
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"_Totals: {len(systems)} systems · {total_topics} topics · {total_versions} versions · {total_hy} high-yield_"
    )
    lines.append("")
    lines.append("## Anki deck")
    lines.append("")
    lines.append(
        "Two ways to get started — pick one now, add more later. Every download stays "
        "in sync: cards you've already reviewed keep your progress, and nothing gets "
        "duplicated when you import an updated or additional deck."
    )
    lines.append("")
    lines.append(
        "- **Everything at once** — "
        f"[{ANKI_ICON}`im-rec-deck.apkg`](https://github.com/cfu288/im-rec-deck/raw/main/build/im-rec-deck.apkg) "
        f"({total_versions} guidelines, {total_topics} topics)."
    )
    lines.append(
        "- **One guideline at a time** — open any system from the left sidebar, then "
        f"click the **{ANKI_ICON}Anki deck** link next to the guideline you want."
    )
    lines.append("")
    lines.append("In Anki: **File → Import**.")
    lines.append("")
    lines.append(
        "**Don't grind the whole deck dry.** It's broad on purpose (every topic above, "
        "every flagged recommendation), and front-loading the long tail will bury you. "
        "Suggested first pass:"
    )
    lines.append("")
    lines.append(
        "1. **Suspend dosing cards.** Specific drug doses are reference-lookup material, "
        "not spaced-repetition material. In the Anki browser, search "
        "`deck:\"IMRecDeck\" tag:im-rec-deck::dosing` → select all → "
        "**Notes → Suspend**."
    )
    lines.append(
        f"2. **Suspend non-high-yield cards.** Start with the ~{total_hy} ⭐ topics. "
        "Search "
        "`deck:\"IMRecDeck\" -tag:im-rec-deck::high-yield` → select "
        "all → **Notes → Suspend**."
    )
    lines.append(
        "3. Unsuspend the rest as you encounter the underlying topics on service or in "
        "study blocks."
    )
    lines.append("")
    sg = manifest.get("study_guides") or {}
    if sg:
        lines.append("## Study guides")
        lines.append("")
        for slug, body in sg.items():
            title = body.get("title", slug)
            href = site_url(f"/study-guides/{slug}/")
            entry = f"- [{title}]({href})"
            if body.get("description"):
                entry += f" — {body['description']}"
            lines.append(entry)
        lines.append("")
    return "\n".join(lines) + "\n"


def render_version_page(
    system_slug: str,
    system_title: str,
    topic_slug: str,
    topic_title: str,
    v: dict,
    topic_society: str | None,
) -> str | None:
    """Publish the enriched body of a version as its own deep-dive page under
    /{system}/{topic}/{version_slug}/. Returns None when the reference file
    doesn't exist or is still a skeleton (so we don't publish empty pages)."""
    vslug = version_slug(v, topic_society)
    ref = REFERENCES / system_slug / topic_slug / f"{vslug}.md"
    if not ref.is_file():
        return None
    text = ref.read_text()
    m = FRONTMATTER_RE.match(text)
    body = (m.group(2) if m else text).strip()
    if SKELETON_BODY_MARKER in body or "# Key Recommendations" not in body:
        return None

    year = v.get("year")
    society = v.get("society") or topic_society
    title = v.get("title") or f"{topic_title} — {vslug}"
    subtitle = fmt_year_society(year, society)

    # Publisher links row — same set the system-page bullet has, promoted to
    # first-class on the deep-dive header since this page is where a reader
    # who wants the primary source would jump out. See render_version() for
    # the same collapse-canonical-into-html rationale.
    src = v.get("source") or {}
    format_urls: dict[str, str] = {}
    if isinstance(src, dict):
        for fmt in ("html", "pdf", "epub", "pmc", "xml"):
            entry = src.get(fmt)
            if isinstance(entry, dict) and entry.get("url"):
                format_urls[fmt] = entry["url"]
    if v.get("url") and "html" not in format_urls:
        format_urls["html"] = v["url"]
    url_links = [f"[{fmt}]({url})" for fmt, url in format_urls.items()]
    if v.get("pmid"):
        url_links.append(f"[PubMed](https://pubmed.ncbi.nlm.nih.gov/{v['pmid']}/)")

    # just-the-docs nesting: version pages sit under their topic index page,
    # which sits under the system. Without grand_parent, both topic and version
    # pages collapse into the same sidebar level and the system page renders
    # two parallel lists of children (topics AND versions).
    header_lines = [
        "---",
        # YAML titles can contain colons / quotes — quote to be safe.
        "title: " + repr(subtitle),
        f"parent: {topic_title!r}",
        f"grand_parent: {system_title!r}",
        f"permalink: /{system_slug}/{topic_slug}/{vslug}/",
        "---",
        "",
        f"**{subtitle}** · {topic_title}",
        "",
    ]
    if url_links:
        header_lines.append("**Read the guideline:** " + " · ".join(url_links))
        header_lines.append("")
    # Per-guideline Anki sub-deck (produced by build_apkg.py). Stable GUIDs +
    # deck IDs mean a user can import this on its own OR alongside the mega
    # deck without duplicate notes / dupe deck tree / lost FSRS history.
    # Only card_eligible versions have a deck; a superseded version's cards are
    # retired, so its deep-dive page must not link a now-orphaned .apkg.
    is_card_eligible = bool(m) and "card_eligible: true" in m.group(1)
    if is_card_eligible:
        subdeck_url = (
            "https://github.com/cfu288/im-rec-deck/raw/main/build/decks/"
            f"{system_slug}/{topic_slug}/{vslug}.apkg"
        )
        header_lines.append(
            f"[{ANKI_ICON}Download this guideline's Anki deck (.apkg)]({subdeck_url})"
        )
        header_lines.append("")
    # Extra "\n" gives a blank line between the header block and the body so
    # kramdown renders "# Summary" as its own paragraph, not text-flow.
    return "\n".join(header_lines) + "\n" + body + "\n"


def render_topic_index_page(
    system_slug: str,
    system_title: str,
    topic_slug: str,
    topic_block: dict,
) -> str:
    """Landing page for /<system>/<topic>/. Without this file the folder URL
    404s (Jekyll only emits pages for .md files, and topic folders previously
    contained only per-version files). Users who guess the folder URL, share
    a folder-level link, or land on a stale reference now get a real page
    listing the topic's versions with high-yield / society metadata and a
    direct download for the current guideline's Anki sub-deck when enriched.

    just-the-docs nav placement: parent is the system and has_children: true,
    so version deep-dive pages nest under this topic (via `grand_parent`) and
    the system sidebar shows only topics, not the flat cross-product of topics
    and versions.
    """
    title = topic_block.get("title") or topic_slug
    society = topic_block.get("society")
    high_yield = topic_block.get("high_yield")
    versions = topic_block.get("versions") or []

    fm_title = f"{title} ⭐" if high_yield else title
    lines: list[str] = [
        "---",
        f"title: {fm_title!r}",
        f"parent: {system_title!r}",
        f"permalink: /{system_slug}/{topic_slug}/",
        "has_children: true",
        "---",
        "",
        f"# {title}" + (" ⭐" if high_yield else ""),
        "",
    ]
    meta_parts = [f"`{topic_slug}`"]
    if society:
        meta_parts.append(f"society: **{society}**")
    if high_yield:
        meta_parts.append("**high-yield**")
    lines.append(" · ".join(meta_parts))
    lines.append("")
    lines.append(f"[← back to {system_title}]({system_link(system_slug)})")
    lines.append("")

    summary = reference_summary(system_slug, topic_slug, versions, society)
    if summary:
        lines.append("## Summary")
        lines.append("")
        lines.append(summary)
        lines.append("")

    # Direct sub-deck download for the current version, promoted above the
    # version list so a reader who lands on the topic page and wants the
    # Anki deck for "this topic, current guideline" doesn't have to scan.
    dated = [v for v in versions if v.get("year") is not None]
    current = (
        max(dated, key=lambda v: v["year"]) if dated
        else (versions[0] if versions else None)
    )
    if current is not None:
        vslug = version_slug(current, society)
        ref_path = REFERENCES / system_slug / topic_slug / f"{vslug}.md"
        is_enriched = False
        if ref_path.is_file():
            body = ref_path.read_text()
            m = FRONTMATTER_RE.match(body)
            body_only = m.group(2) if m else body
            if (
                SKELETON_BODY_MARKER not in body_only
                and "# Key Recommendations" in body_only
            ):
                is_enriched = True
        if is_enriched:
            subdeck_url = (
                "https://github.com/cfu288/im-rec-deck/raw/main/"
                f"build/decks/{system_slug}/{topic_slug}/{vslug}.apkg"
            )
            deep_link = site_url(f"/{system_slug}/{topic_slug}/{vslug}/")
            cur_title = current.get("title") or fmt_year_society(
                current.get("year"), current.get("society") or society
            )
            lines.append(
                f"**Current guideline:** [{cur_title}]({deep_link}) · "
                f"[{ANKI_ICON}download Anki sub-deck]({subdeck_url})"
            )
            lines.append("")

    lines.append("## Versions")
    lines.append("")
    if not versions:
        lines.append("_No versions listed._")
    else:
        for v in versions:
            lines.extend(render_version(v, society, system_slug, topic_slug))
    lines.append("")
    return "\n".join(lines) + "\n"


STUDY_GUIDE_DIR = REFERENCES / "study-guides"
# Rewrite "](/sys/topic/)" → Liquid relative_url so the link resolves on both
# project sites (https://user.github.io/repo/) and root-domain hosting.
INTERNAL_LINK_RE = re.compile(r"\]\((/[a-z][a-z0-9-]*/[a-z0-9-]+/)\)")


def render_study_guide(slug: str, sg_block: dict) -> str | None:
    """Read the matching study-guide reference file, rewrite its internal
    topic links to be baseurl-aware, and wrap in Jekyll frontmatter so it
    becomes a real page at /study-guides/<slug>/."""
    src = STUDY_GUIDE_DIR / f"{slug}.md"
    if not src.is_file():
        return None
    text = src.read_text()
    m = FRONTMATTER_RE.match(text)
    body = (m.group(2) if m else text).strip()
    body = INTERNAL_LINK_RE.sub(
        lambda mm: "](" + site_url(mm.group(1)) + ")", body
    )
    fm_lines = [
        "---",
        f"title: {sg_block.get('title', slug)!r}",
        f"permalink: /study-guides/{slug}/",
        "parent: 'Study guides'",
        "---",
        "",
    ]
    return "\n".join(fm_lines) + body + "\n"


def render_study_guides_parent(count: int, systems_count: int) -> str:
    """Landing page for the Study guides nav section (parent of each
    individual guide). just-the-docs needs a parent page so has_children
    resolves correctly and the section is browsable directly."""
    lines = [
        "---",
        "title: 'Study guides'",
        "permalink: /study-guides/",
        f"nav_order: {systems_count + 2}",  # after Home + all system pages
        "has_children: true",
        "---",
        "",
        "# Study guides",
        "",
        "Curated cross-cutting lists that pull together the most important",
        "material from across systems.",
    ]
    return "\n".join(lines) + "\n"


def main():
    with MANIFEST.open() as f:
        manifest = yaml.load(f)
    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "index.md").write_text(render_index(manifest))
    # Assign nav_order to each system alphabetically, starting at 2 (Home is 1).
    # SYSTEM_NAV_ORDER is read by render_system.
    for i, system_slug in enumerate(sorted((manifest.get("systems") or {}).keys()), start=2):
        SYSTEM_NAV_ORDER[system_slug] = i
    n_systems = 0
    n_versions = 0
    n_topic_indexes = 0
    for system_slug, sys_block in (manifest.get("systems") or {}).items():
        (DOCS / f"{system_slug}.md").write_text(render_system(system_slug, sys_block))
        n_systems += 1
        system_title = sys_block.get("title", system_slug)
        for topic_slug, topic_block in (sys_block.get("topics") or {}).items():
            topic_title = topic_block.get("title", topic_slug)
            topic_society = topic_block.get("society")
            # Topic-level index.md so /<system>/<topic>/ resolves to a real
            # page (was 404 previously — no file at the folder level).
            topic_dir = DOCS / system_slug / topic_slug
            topic_dir.mkdir(parents=True, exist_ok=True)
            (topic_dir / "index.md").write_text(
                render_topic_index_page(
                    system_slug, system_title, topic_slug, topic_block
                )
            )
            n_topic_indexes += 1
            for v in topic_block.get("versions") or []:
                page = render_version_page(
                    system_slug, system_title, topic_slug, topic_title, v, topic_society
                )
                if page is None:
                    continue
                vslug = version_slug(v, topic_society)
                (topic_dir / f"{vslug}.md").write_text(page)
                n_versions += 1
    n_sg = 0
    sg_out = DOCS / "study-guides"
    sg_map = manifest.get("study_guides") or {}
    if sg_map:
        sg_out.mkdir(parents=True, exist_ok=True)
        (sg_out / "index.md").write_text(
            render_study_guides_parent(len(sg_map), n_systems)
        )
    for slug, body in sg_map.items():
        page = render_study_guide(slug, body)
        if page is None:
            continue
        (sg_out / f"{slug}.md").write_text(page)
        n_sg += 1
    print(
        f"wrote docs/index.md, {n_systems} per-system pages, "
        f"{n_topic_indexes} per-topic index pages, "
        f"{n_versions} per-version deep-dive pages, {n_sg} study-guide pages"
    )


if __name__ == "__main__":
    main()
