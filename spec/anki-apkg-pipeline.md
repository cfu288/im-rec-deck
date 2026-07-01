---
type: Reference Doc
title: Anki .apkg Pipeline
description: How we build an .apkg containing a custom GuidelinesCloze notetype + cards, using genanki.
---

# Anki .apkg Pipeline

The pipeline emits a single `.apkg` containing both the custom `GuidelinesCloze` notetype and all generated cards. One file, one import in Anki; notetype + template + CSS install automatically alongside the notes.

A `.apkg` is a zipped SQLite collection. When imported, Anki installs any notetypes it references and adds the included notes. It's the canonical way to ship a deck with its own model.

Why this matters over a plain TSV import: a TSV can only *reference* a notetype that already exists in the user's collection — it can't create one. Custom notetypes like ours (with first-class Source/System/Society/Year fields, a styled template, and per-card CSS) need a `.apkg` to install themselves on first import.

## Library: `genanki`

`genanki` (https://github.com/kerrickstaley/genanki) is the standard Python library for building `.apkg` files. Pure-Python, no Anki running required, no AnkiConnect, idempotent if you control GUIDs.

### Core API (verified against source 2026-06)

#### `genanki.Model`

```python
def __init__(
    self,
    model_id=None,            # required; stable random int in [1<<30, 1<<31)
    name=None,                # display name shown in Anki
    fields=None,              # list of {'name': str}
    templates=None,           # list of {'name', 'qfmt', 'afmt'}
    css='',                   # optional CSS string
    model_type=FRONT_BACK,    # FRONT_BACK = 0, CLOZE = 1
    latex_pre=DEFAULT_LATEX_PRE,
    latex_post=DEFAULT_LATEX_POST,
    sort_field_index=0,       # which field shows in browser by default
)
```

**Critical:** for cloze cards the model needs `model_type=genanki.Model.CLOZE` (= 1). Default is FRONT_BACK, which silently drops `{{cloze:Text}}` rendering.

#### `genanki.Note`

```python
def __init__(
    self,
    model=None,
    fields=None,              # list of strings, one per model field, in order
    sort_field=None,
    tags=None,                # list of strings; "::" denotes hierarchy
    guid=None,                # explicit GUID; defaults to hash of all fields
    due=0,
)
```

**Pass `guid` explicitly.** The default (`None`) calls `guid_for(*self.fields)` which hashes every field value — any text edit invents a new note and forfeits review history. Use a stable manifest-derived key.

#### `genanki.Deck` and `genanki.Package`

```python
deck = genanki.Deck(deck_id, name)   # deck_id same constraints as model_id
deck.add_note(note)
genanki.Package(deck).write_to_file('output.apkg')
```

Multiple decks: `genanki.Package([deck1, deck2])`.

### Stable IDs

Per the README, generate `model_id` and `deck_id` once with `random.randrange(1 << 30, 1 << 31)` and hardcode the integers. Reuse on every regeneration so Anki recognizes the model/deck across reimports.

In our code these are constants:

```python
MODEL_ID = 1_602_734_115   # generated once, never change
DECK_ID  = 2_059_400_111
```

If you ever do change `MODEL_ID`, you've effectively defined a *new* notetype. Existing notes attached to the old `MODEL_ID` are orphaned in Anki's database (the notetype stays in the collection but is no longer the target of new imports).

### Update / reimport semantics

> "If you import a new note that has the same GUID as an existing note, the new note will overwrite the old one (as long as their models have the same fields)."

— genanki README

Two consequences for us:

1. **GUIDs must be stable across regenerations.** We derive them from the manifest key (`{system}/{topic}/{version}::{card_key}`). Cards with the same GUID across runs update in place; review history is preserved.
1. **Model fields can't drift on reimport.** If you change the fields list (add/rename/remove) and reimport, Anki refuses to overwrite — you get duplicates. Treat the fields schema as a one-way commitment. If you must change fields, do a one-time migration (manual UI edit on the notetype in Anki) before the next import.

## Our `GuidelinesCloze` model

### Fields

| #   | Name         | Purpose                                                            |
| --- | ------------ | ------------------------------------------------------------------ |
| 1   | `Text`       | The cloze-formatted sentence (must be first for `{{cloze:Text}}`). |
| 2   | `Back Extra` | Optional extra context shown on the back. Often empty.             |
| 3   | `Source`     | The source citation (section / table / recommendation number).     |
| 4   | `System`     | Body system slug (e.g. `cardiology`).                              |
| 5   | `Topic`      | Topic slug (e.g. `hypertension`).                                  |
| 6   | `Society`    | Issuing society as it appears in the manifest (e.g. `AHA/ACC`).    |
| 7   | `Year`       | Year of the version, integer as string. Empty for living docs.     |

The `_meta` / tag info is duplicated into first-class fields so the Anki browser shows them as columns and you can sort / filter by them without parsing tag strings.

### Cards template

One cloze template; conditional sections so empty fields don't render as labels.

**Front:**

```html
{{cloze:Text}}
```

**Back:**

```html
{{cloze:Text}}

{{#Back Extra}}<div class="back-extra">{{Back Extra}}</div>{{/Back Extra}}

<hr>
<div class="meta">
  <div class="source"><b>Source:</b> {{Source}}</div>
  <div class="provenance">
    {{Society}}{{#Year}} · {{Year}}{{/Year}} · <span class="path">{{System}} / {{Topic}}</span>
  </div>
</div>
```

The `{{#FieldName}}...{{/FieldName}}` blocks only render when the field is non-empty (standard Anki template syntax).

### CSS

Keep it modest — readable type, separator below the cloze answer, dimmed metadata.

```css
.card { font-family: -apple-system, system-ui, sans-serif; font-size: 20px; text-align: left; color: #222; background: #fafafa; padding: 1em; }
.cloze { color: #d33; font-weight: 600; }
.back-extra { margin: 0.75em 0; color: #444; }
hr { border: 0; border-top: 1px solid #ddd; margin: 1em 0 0.5em; }
.meta { font-size: 0.78em; color: #777; line-height: 1.4; }
.source { margin-bottom: 0.25em; }
.provenance .path { font-family: ui-monospace, monospace; color: #999; }
```

## GUID model

```python
guid = sha1(f"{system}/{topic}/{version}::{card_key}".encode()).hexdigest()[:16]
```

Derived from manifest coordinates + a stable per-card key the generator emits. **Not** derived from the cloze text — text edits leave identity untouched, so prose tweaks update existing notes instead of forking them.

## Pipeline shape

```
scripts/build_apkg.py reads:
  build/cards.jsonl          (card data, GUID + Text + Source + tags + _meta)
  references/guidelines/**   (concept frontmatter for Society / Year lookup)

scripts/build_apkg.py writes:
  build/im-rec-deck.apkg      (importable artifact)
```

No API calls. Pure local. Idempotent: run any time after `generate_cards.py` to refresh the `.apkg` against the current `build/cards.jsonl`.

## Open questions

- **Empty-tags edge case.** If a generated card has no tags, does genanki write an empty tag string that Anki's importer then handles cleanly? Expect yes — the field is just a string list. Test with one note before assuming.
- **Field rename migration.** If we ever rename `Source` → `Citation`, the existing-collection migration story isn't tested. The safe path is to keep the field list strictly additive; never rename or remove.
- **Sort field index.** We default to `sort_field_index=0` (the `Text` field). The cloze syntax `{{c1::...}}` will appear literally in the browser sort column. If that's noisy, change to a metadata field like `Source` (index 2).
- **Media** (images, audio) — out of scope for now. If we later embed images from guideline figures, add via `Package.media_files`. Filenames must be basenames only and globally unique.
