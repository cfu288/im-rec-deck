# Architecture Review — imrecdeck

*Generated 2026-06-21. Combines a self-review with an independent codex review of `scripts/`, `spec/`, `manifest.yaml`, `.claude/`, `references/`, `build/`, `sources/`.*

______________________________________________________________________

## Data flow (one-page summary)

```
HAND-CURATED                                      AUTO-GENERATED
═══════════════════════════════════════════════════════════════════
manifest.yaml ──────────────┐
sources/**/*.pdf|html|epub  │   (manually dropped, or downloaded via scripts)
spec/ (rules + conventions) │   (mostly; one auto-generated exception below)
.env (API keys)             │
                            ▼
                  validate_manifest.py  (hook — runs on every manifest edit)
                            │
                            ▼
                  build_references.py  →  references/guidelines/**/*.md  (skeleton)
                            │              + auto-derived study-guides/highest-yield-named-guidelines.md
                            ▼
                  parse_sources.py   →   sources/**/*.md  (unified: epub > html > pdf;
                                                            EPUB/HTML local free, PDF via LlamaParse — PAID)
                                         provenance marker tracks source format + hash
                            │
                            ▼
                  enrich_references.py  →  enriched bodies + _source_hash  (Anthropic batch — PAID)
                                         auto-resumes prior batches via build/.enrich-state.json
                            │
                            ▼
                  generate_cards.py     →  build/cards.jsonl  (Anthropic batch — PAID)
                            │
                            ▼
                  build_apkg.py         →  build/imrecdeck.apkg
                                                  │
                                                  ▼
                                            USER IMPORTS INTO ANKI
```

## What's manual vs automated

| Hand-curated                                                                | Auto-generated                                                                                      |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `manifest.yaml` (the catalog)                                               | `sources/**/*.md` (parsed sidecars)                                                                 |
| `sources/**/*.pdf`/`.html`/`.epub` (raw documents — dropped or downloaded)  | `references/guidelines/**` (entire OKF bundle)                                                      |
| `spec/conventions.md`, `spec/anki-*.md`, `spec/knowledge-catalog/` (cloned) | `spec/manifest-needs-attention.md` (written by `needs_attention_report.py` — exception to the rule) |
| `.env`                                                                      | `build/cards.jsonl`, `build/imrecdeck.apkg`                                                         |
| Importing into Anki                                                         | All scripts in `scripts/`                                                                           |

## Single sources of truth

| Subject                           | Source                                                |
| --------------------------------- | ----------------------------------------------------- |
| Which guidelines exist + versions | `manifest.yaml`                                       |
| Source documents                  | `sources/`                                            |
| High-yield flagging               | `topic.high_yield: true` in `manifest.yaml`           |
| Card body content                 | `build/cards.jsonl` (model-generated, frozen)         |
| Card tags                         | Derived at output time from manifest + concept path   |
| Card identity (GUID)              | `sha1("{system}/{topic}/{version}::{card_key}")[:16]` |
| Notetype + deck IDs (Anki)        | Hardcoded constants in `build_apkg.py`                |

## Script inventory

| Pipeline-core (I wrote) | Purpose                                                                                                                                                              |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `validate_manifest.py`  | Pydantic check on manifest schema (runs on every edit via hook)                                                                                                      |
| `build_references.py`   | manifest → skeleton OKF bundle; preserves `_source_hash` + bodies                                                                                                    |
| `parse_sources.py`      | Unified parser; picks best format per concept (epub > html > pdf); pays LlamaParse only when no better format exists; HTML-comment provenance marker for idempotency |
| `enrich_references.py`  | Anthropic batch — extracts structured bodies. Auto-resumes prior batches from `build/.enrich-state.json`; chunked (≤20 per submit) to bound exposure                 |
| `generate_cards.py`     | Anthropic batch — concept bodies → cloze cards. Chunked submission; body-hash + generator-version per card detect stale concepts and force regen                     |
| `build_apkg.py`         | cards.jsonl → .apkg (custom GuidelinesCloze notetype + cards in one importable file)                                                                                 |

| Download / scraping subsystem (added out-of-band) | Purpose                                                                                                 |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `download_format_aware.py`                        | Format-aware downloader (current)                                                                       |
| `merge_format_urls.py`                            | URL discovery / merge into manifest                                                                     |
| `fix_format_urls.py`                              | URL repair helper                                                                                       |
| `import_manual_downloads.py`                      | Import manually-dropped files (from `tmp/`) into manifest + sources/                                    |
| `epub_wrap.py`                                    | EPUB harvesting helper (paired with `puppeteer_epub.js`)                                                |
| `scrub_false_epubs.py`                            | Quarantine downloaded files that claim `.epub` but aren't                                               |
| `list_pdfs_to_parse.py`                           | Priority queue for parsing — high-yield topics first                                                    |
| `needs_attention_report.py`                       | Generates `spec/manifest-needs-attention.md` curation report                                            |
| `build_launchpad.py`                              | Generates `/tmp/epub-launchpad.html` (article URLs grouped by publisher) for the manual EPUB harvest UI |
| `puppeteer_epub.js`, `open_browser.js`            | JS helpers (paired with `epub_wrap.py` / `build_launchpad.py`)                                          |

## Idempotency / cache state

| Script                 | Skip rule                                                                                                                                                                                       |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `parse_sources.py`     | Provenance marker on `.md` matches `(best-source-format, source-sha256)`; unmarked legacy `.md`s are left alone unless a strictly better format is now on disk                                  |
| `enrich_references.py` | `_source_hash` in frontmatter matches current source content hash; also auto-resumes prior batches from `build/.enrich-state.json`                                                              |
| `generate_cards.py`    | `(system, topic, version)` covered AND stored `body_hash` matches the concept's current body hash AND stored `generator_version == GENERATOR_VERSION`. Stale concepts are purged + regenerated. |
| `build_references.py`  | Preserves `_source_hash` + body on existing files; no skip but no destruction either                                                                                                            |
| `build_apkg.py`        | None (cheap, fully derivable)                                                                                                                                                                   |

## Automation in `.claude/`

- **Hook**: `validate_manifest.py` runs on every Edit/Write of `manifest.yaml`
- **Hook**: mdformat runs on every Edit/Write of `*.md` (excluding `spec/knowledge-catalog/*`)
- **Skill**: `parse-sources` — wraps `parse_sources.py`, invokable as `/parse-sources`

______________________________________________________________________

## Problem list

### ✅ Resolved (this session)

- **`build_references.py` stale "wipes the bundle" docstring** — updated.
- **Parser race + wasted LlamaParse credits.** Merged `parse_sources.py` + `parse_alt_formats.py` into one deterministic script with format preference (epub > html > pdf) and an HTML-comment provenance marker for idempotency. `parse_alt_formats.py` deleted.
- **No auto-recovery from canceled enrichment batches.** `enrich_references.py` now writes `build/.enrich-state.json` at submit time and auto-resumes any unfingested prior batch on the next run. `salvage_batch.py` deleted.
- **TSV path obsolete.** `export_anki_tsv.py`, `build/cards.tsv`, and `spec/anki-import-format.md` deleted. apkg is the sole import path. Cross-import duplication risk is dead with it.
- **Legacy schema writers.** `download_sources.py`, `puppeteer_wrap.py`, `puppeteer_download.js`, and `migrate_source_schema.py` deleted; `download_format_aware.py` covers their use cases.
- **Hardcoded absolute repo paths.** All 10 scripts swept; replaced with `Path(__file__).resolve().parent.parent` (and the JS equivalent in `open_browser.js`).
- **`list_pdfs_to_parse.py` dead priority feature.** Rewritten to read `topic.high_yield: true` directly from the manifest (the new single source of truth).
- **Anthropic batch cancel as a budget guard.** `enrich_references.py` and `generate_cards.py` now submit in chunks of ≤20 — exposure per submit is bounded; state-file resume covers Ctrl-C between chunks. Pre-flight cost estimate printed before any submission.
- **Card-gen body-change blindness.** `generate_cards.py` stores `body_hash` + `generator_version` in each card's `_meta` and forces a regen + purge for concepts whose body or generator drifted.
- **No pipeline orchestrator.** Added `Justfile` at the repo root (run `just` to list targets). `all-local` runs only free local steps; paid steps (`enrich`, `cards`) are never part of `all-*` and must be invoked explicitly.
- **No top-level entry doc.** Added `README.md` covering the data flow, quickstart, and invariants.
- **`needs_attention_report.py` docstring lied about output path.** Updated to reflect that it writes `spec/manifest-needs-attention.md` (not `/tmp`).

### Open / deferred

1. **No orphan cleanup for renamed/removed topics.** `build_references.py` preserves enriched concepts but doesn't delete files for topics that no longer exist in the manifest. Acceptable as long as manifest renames are rare; eventually add `--report-orphans` (default) and `--prune` (explicit) flags. Deferred until manifest shape is more stable.

1. **`build_launchpad.py`** has a docstring but the workflow it participates in (paired with `open_browser.js` and `import_manual_downloads.py`) isn't documented end-to-end anywhere. Low priority — operator knows the flow.

1. **`spec/manifest-needs-attention.md` location convention.** File stays under `spec/` (it's a human-readable curator report, not a build artifact) — but `spec/conventions.md` should mention this exception once the next layout cleanup happens. Cosmetic.

______________________________________________________________________

## What each reviewer caught the other missed

**Codex found that I missed:** the ~9 download/scraping scripts (entire subsystem), the legacy schema-writer bug, the dead priority feature in `list_pdfs_to_parse.py`, the auto-generated `spec/manifest-needs-attention.md` exception, and the stale `build_references.py` docstring. My review trusted my mental model of what I had built; codex actually scanned the directory.

**I found that codex missed:** the `card_key`-as-GUID-input fragility under force-regen, and the painful history of the now-removed `shutil.rmtree` in `build_references.py`. (The TSV+apkg cross-import problem I also caught is now moot — the TSV path was deleted as part of the synthesis plan.)

Codex's review was broader and more accurate for this repo because half the scripts were added out-of-band.
