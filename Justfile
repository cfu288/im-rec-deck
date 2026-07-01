# Pipeline orchestration. Run `just` to see all targets.
#
# IMPORTANT: paid targets (`enrich`, `cards`) hit the Anthropic API and cost
# money. They are deliberately NOT part of any `all-*` chain. Run them
# explicitly when you mean to.

# Show available targets when run with no arguments.
default:
    @just --list

# ── Free, local-only ──────────────────────────────────────────────────────

# Validate manifest.yaml against the Pydantic schema.
validate:
    uv run scripts/validate_manifest.py

# Parse sources/**/*.{epub,html,pdf} → sibling .md (epub > html > pdf).
# Only PDF parsing hits LlamaParse (paid); the rest is local.
parse:
    uv run scripts/parse_sources.py

# Build references/guidelines/ skeleton from manifest.yaml. Preserves any
# existing _source_hash + enriched bodies.
build:
    uv run scripts/build_references.py

# Generate spec/manifest-needs-attention.md report.
report:
    uv run scripts/needs_attention_report.py

# Package build/cards.jsonl + manifest into build/imrecdeck.apkg.
apkg:
    uv run scripts/build_apkg.py

# Heuristic spoiler-pattern check on build/cards.jsonl.
# Catches: NUMERIC_OVERLAP, NUMERIC_BOUNDARY, LITERAL_REPETITION, GRAMMAR_TELL.
# Report-only; add `--strict` to exit non-zero.
qa-cards:
    uv run scripts/validate_cards.py

# Regenerate docs/index.md + docs/<system>.md from manifest.yaml.
# The deck download links to build/imrecdeck.apkg on GitHub directly (build/
# is committed), so nothing to stage into docs/.
publish-docs:
    uv run scripts/build_docs.py

# Everything free: validate → parse → build → report → apkg → qa-cards.
# Safe to run any time; never spends money on API.
all-local: validate parse build report apkg qa-cards

# ── Paid (Anthropic API spend) ─────────────────────────────────────────────

# Enrich card-eligible concepts with structured bodies via Anthropic batch.
# Idempotent: skips concepts whose _source_hash matches their current source.
# Auto-resumes any prior interrupted batches from build/.enrich-state.json.
# Chunks submissions in groups of 20 to bound exposure per submit.
enrich:
    uv run scripts/enrich_references.py

# Generate Anki cloze cards from enriched concepts via Anthropic batch.
# Idempotent: skips concepts whose body hash + generator_version match.
# Chunks submissions in groups of 20.
cards:
    uv run scripts/generate_cards.py

# LLM-based deep audit of multi-cloze cards for spoiler patterns.
# Uses Haiku (cheap; ~$0.10-0.20 per full run). Catches semantic leaks the
# heuristic validator can't see. Report at build/qa-cards-deep.jsonl.
qa-cards-deep:
    uv run scripts/validate_cards_deep.py

# Classify each card as dosing vs concept; writes build/card-classifications.jsonl.
# build_apkg.py reads it and adds `imrecdeck::dosing` tag to flagged cards.
# Cheap Haiku run (~$0.05).
qa-dosing:
    uv run scripts/classify_dosing.py
