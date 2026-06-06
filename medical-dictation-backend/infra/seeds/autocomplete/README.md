# Autocomplete corpus

Migration `0026_seed_autocomplete_system_corpus.sql` inlines a starter
corpus (~30 phrases + 7 snippets across UK + EN). The
production-grade corpus (~10k phrases, ~60 snippets) is a
clinical-content-lead deliverable that lands incrementally — drop CSV
files (`phrases_uk.csv`, `phrases_en.csv`) and JSON files
(`snippets_uk.json`, `snippets_en.json`) here for the migration to
ingest.

## Format

`phrases_*.csv`:

```
phrase,language,specialty,section_hint
"задишка при фізичному навантаженні",uk,cardiology,anamnesis
```

`snippets_*.json`:

```json
[
  {"trigger": "cv", "expansion": "...", "cursor_position": 50, "language": "uk"}
]
```

## CI gate

`scripts/validate-autocomplete-corpus.py` runs on every PR touching
this directory; rejects phrases > 80 chars, IPN-like 10-digit numbers,
emails, ASCII apostrophes in Ukrainian phrases, duplicate keys.
