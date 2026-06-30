# /// script
# requires-python = ">=3.10"
# dependencies = ["pydantic", "pyyaml"]
# ///
"""Build references/guidelines/ OKF bundle from manifest.yaml (Stage 1: skeleton).

Re-runnable. Overwrites manifest-derived fields in place but PRESERVES any
existing `_source_hash` + enriched body so enrichment work isn't destroyed by
a skeleton rebuild. Does NOT delete files for topics that were renamed or
removed in the manifest — clean those up manually via `git status` review.
Bodies for unenriched concepts are minimal: frontmatter + the manifest `notes`
field if present. Stage 2 (body enrichment from parsed sources) is a separate
script.

Usage:
    uv run scripts/build_references.py
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from validate_manifest import Manifest, System, Topic, Version


BUNDLE_ROOT = Path("references/guidelines")
TIMESTAMP = date.today().isoformat()
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)


def read_existing(path: Path) -> tuple[dict, str]:
    """Return (frontmatter, body) for an existing concept file, or ({}, '')."""
    if not path.is_file():
        return {}, ""
    text = path.read_text()
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, ""
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, m.group(2)


def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\s*&\s*", "-and-", s)
    s = re.sub(r"[/\s]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


def version_slug(version: Version, topic_society: Optional[str]) -> str:
    society = version.society or topic_society
    society_part = slugify(society) if society else "unknown"
    if version.year is not None:
        return f"{version.year}-{society_part}"
    return society_part


def write_md(path: Path, frontmatter: dict, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_block = yaml.safe_dump(
        frontmatter, sort_keys=False, allow_unicode=True
    ).strip()
    path.write_text(f"---\n{yaml_block}\n---\n\n{body}".rstrip() + "\n")


def current_version(topic: Topic) -> Optional[Version]:
    """Pick the version this topic represents as 'current'.

    Year-pinned: the latest dated version.
    Yearless (live guidelines — HCV AASLD/IDSA, CDC/ACIP, NIH COVID-19, etc.):
      the first listed version — manifest order is intentional and
      authoritative when no year is present.

    Without the yearless fallback, every topic whose newest version lacks a
    year silently drops out of card_eligible, the deck, and docs summaries —
    even when it's high-yield (e.g., the CDC adult immunization schedule).
    """
    dated = [v for v in topic.versions if v.year is not None]
    if dated:
        return max(dated, key=lambda v: v.year)
    return topic.versions[0] if topic.versions else None


def build_bundle_root_index(manifest: Manifest) -> None:
    lines = ["# Internal Medicine Guidelines", ""]
    lines.append("Generated from `/manifest.yaml`. Do not edit by hand.")
    lines.append("")
    for sys_slug, system in manifest.systems.items():
        desc = system.description or ""
        lines.append(f"- [{system.title}]({sys_slug}/) — {desc}".rstrip(" —"))
    if manifest.study_guides:
        lines.append("- [Study guides](study-guides/) — Cross-cutting curated lists.")
    write_md(
        BUNDLE_ROOT / "index.md",
        {
            "type": "Knowledge Bundle",
            "title": "Internal Medicine Guidelines",
            "description": "System-by-system index of current society guidelines for internal medicine practice.",
            "okf_version": "0.1",
            "timestamp": TIMESTAMP,
        },
        "\n".join(lines),
    )


def build_system_index(sys_slug: str, system: System) -> None:
    lines = [f"# {system.title}", ""]
    if system.description:
        lines.append(system.description)
        lines.append("")
    for topic_slug, topic in system.topics.items():
        cur = current_version(topic)
        if cur:
            label = f"{cur.year} {cur.society or topic.society or ''}".strip()
            lines.append(f"- [{topic.title}]({topic_slug}/) — current: {label}")
        else:
            lines.append(f"- [{topic.title}]({topic_slug}/) — living")
    write_md(
        BUNDLE_ROOT / sys_slug / "index.md",
        {
            "type": "Bundle Section",
            "title": system.title,
            "description": system.description
            or f"Society guidelines for {system.title.lower()}.",
        },
        "\n".join(lines),
    )


def build_topic_index(sys_slug: str, topic_slug: str, topic: Topic) -> None:
    dated = sorted(
        (v for v in topic.versions if v.year is not None),
        key=lambda v: v.year,
        reverse=True,
    )
    living = [v for v in topic.versions if v.year is None]
    cur = dated[0] if dated else None

    lines = [f"# {topic.title}", ""]
    if cur:
        slug = version_slug(cur, topic.society)
        lines.append(f"Current: [{cur.title or slug}]({slug}.md)")
        lines.append("")
    lines.append("# Versions")
    # Match mdformat's canonical "blank line before list under a heading" so
    # rerunning the script doesn't churn against the pre-commit-hook-formatted
    # files already in git.
    lines.append("")
    for v in dated:
        slug = version_slug(v, topic.society)
        label = v.title or f"{v.year} {v.society or topic.society or ''}".strip()
        marker = "current" if v is cur else "superseded"
        lines.append(f"- [{label}]({slug}.md) — {marker}")
    for v in living:
        slug = version_slug(v, topic.society)
        label = v.title or v.society or slug
        lines.append(f"- [{label}]({slug}.md) — living")

    frontmatter: dict = {
        "type": "Guideline Topic",
        "title": topic.title,
        "system": sys_slug,
    }
    if topic.society:
        frontmatter["society"] = topic.society
    if cur:
        cur_slug = version_slug(cur, topic.society)
        frontmatter["current_version"] = f"/{sys_slug}/{topic_slug}/{cur_slug}.md"

    write_md(
        BUNDLE_ROOT / sys_slug / topic_slug / "index.md",
        frontmatter,
        "\n".join(lines),
    )


def build_version_concept(
    sys_slug: str,
    topic_slug: str,
    topic: Topic,
    version: Version,
    is_current: bool,
) -> None:
    slug = version_slug(version, topic.society)
    society = version.society or topic.society

    frontmatter: dict = {
        "type": "Clinical Guideline",
        "title": version.title or f"{topic.title} — {slug}",
    }
    if society:
        frontmatter["society"] = society
    if version.year is not None:
        frontmatter["year"] = version.year
    if version.url:
        frontmatter["url"] = version.url
    if version.pmid:
        frontmatter["pmid"] = version.pmid
    if version.source:
        frontmatter["source"] = {
            fmt: {k: v for k, v in entry.model_dump().items() if v is not None}
            for fmt, entry in version.source.items()
        }
    frontmatter["card_eligible"] = is_current
    if version.needs_attention:
        frontmatter["needs_attention"] = version.needs_attention

    target = BUNDLE_ROOT / sys_slug / topic_slug / f"{slug}.md"

    # Preserve enrichment state. We check the body content itself rather than
    # only the `_source_hash` frontmatter marker — historical lesson: a missing
    # marker is not a reliable signal that the body is throwaway, and silently
    # clobbering an enriched body (paid Anthropic batch work) on a skeleton
    # rebuild has bitten us before. If the body contains any of the section
    # headers the enrichment script emits, treat it as enriched and preserve.
    existing_fm, existing_body = read_existing(target)
    body_is_enriched = bool(existing_body) and any(
        h in existing_body
        for h in ("# Key Recommendations", "# Thresholds & Doses", "# Citations")
    )
    if body_is_enriched:
        if existing_fm.get("_source_hash"):
            frontmatter["_source_hash"] = existing_fm["_source_hash"]
        # .strip() (not .rstrip()) removes both leading and trailing whitespace.
        # FRONTMATTER_RE's group(2) starts with the \n that separates the closing
        # `---` from the body; without lstripping it, write_md's own `---\n\n{body}`
        # template doubles the blank line, drifting body bytes and busting
        # generate_cards.py's body_hash idempotency on a no-op rebuild.
        body_text = existing_body.strip() + "\n"
    else:
        body_text = "\n".join(
            [
                "# Summary",
                "",
                version.notes or "_Body pending Stage 2 enrichment from parsed source._",
            ]
        )

    write_md(target, frontmatter, body_text)


def high_yield_entries(manifest: Manifest) -> list[tuple[str, str, str]]:
    """Derive (system_slug, topic_slug, topic_title) for every high-yield topic.

    Single source of truth for the high-yield set is `topic.high_yield: true`.
    The study-guide bullet list and the card `high-yield` tag both derive from
    here, so they cannot drift.
    """
    out: list[tuple[str, str, str]] = []
    for sys_slug, system in manifest.systems.items():
        for topic_slug, topic in system.topics.items():
            if topic.high_yield:
                out.append((sys_slug, topic_slug, topic.title))
    return out


def build_study_guides(manifest: Manifest) -> None:
    if not manifest.study_guides:
        return
    sg_dir = BUNDLE_ROOT / "study-guides"

    idx_lines = ["# Study Guides", ""]
    for slug, sg in manifest.study_guides.items():
        idx_lines.append(f"- [{sg.title}]({slug}.md) — {sg.description or ''}".rstrip(" —"))
    write_md(
        sg_dir / "index.md",
        {"type": "Bundle Section", "title": "Study Guides"},
        "\n".join(idx_lines),
    )

    hy_entries = high_yield_entries(manifest)

    for slug, sg in manifest.study_guides.items():
        body = [f"# {sg.title}", ""]
        if sg.description:
            body.append(sg.description)
            body.append("")

        # The "highest-yield-named-guidelines" study guide is auto-derived from
        # topic.high_yield flags so the two lists can never drift. Other study
        # guides (if any) still use their manifest-declared `entries`.
        if slug == "highest-yield-named-guidelines":
            for sys_slug, topic_slug, topic_title in hy_entries:
                body.append(
                    f"- [{topic_title}](/{sys_slug}/{topic_slug}/) — `{sys_slug}::{topic_slug}`"
                )
        else:
            for e in sg.entries:
                body.append(f"- [{e.label}]({e.topic})")

        if sg.bedside_tools:
            body.append("")
            body.append("# Bedside tools")
            body.append("")
            for t in sg.bedside_tools:
                body.append(f"- {t}")
        write_md(
            sg_dir / f"{slug}.md",
            {
                "type": "Study Guide",
                "title": sg.title,
                "description": sg.description or "",
            },
            "\n".join(body),
        )


def main() -> int:
    path = Path("manifest.yaml")
    if not path.is_file():
        print("manifest.yaml not found", file=sys.stderr)
        return 1

    manifest = Manifest.model_validate(yaml.safe_load(path.read_text()))

    # NOTE: do NOT wipe BUNDLE_ROOT — that would destroy enriched concept
    # bodies and their `_source_hash` markers. write_md overwrites in place;
    # build_version_concept preserves _source_hash + body for already-enriched
    # concepts. (Orphan cleanup for renamed/removed topics is not handled here
    # — do it manually with `git status` review after a manifest rename.)
    BUNDLE_ROOT.mkdir(parents=True, exist_ok=True)

    build_bundle_root_index(manifest)
    for sys_slug, system in manifest.systems.items():
        build_system_index(sys_slug, system)
        for topic_slug, topic in system.topics.items():
            build_topic_index(sys_slug, topic_slug, topic)
            cur = current_version(topic)
            for v in topic.versions:
                build_version_concept(sys_slug, topic_slug, topic, v, v is cur)
    build_study_guides(manifest)

    n_sys = len(manifest.systems)
    n_topics = sum(len(s.topics) for s in manifest.systems.values())
    n_versions = sum(
        len(t.versions) for s in manifest.systems.values() for t in s.topics.values()
    )
    print(f"OK: wrote {n_sys} systems, {n_topics} topics, {n_versions} versions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
