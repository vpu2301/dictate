# WER reference corpus

Each subdirectory holds a manifest + audio file pair:

```
<lang>-<specialty>-<id>/
    manifest.json   # see schema below
    audio.wav       # 8 kHz+ PCM/MP3/OGG; ≤ 30 min
```

`manifest.json` schema:

```json
{
  "audio": "audio.wav",
  "language": "uk",
  "specialty": "cardiology",
  "prompt": "Кардіологічна консультація. ...",
  "reference": "<gold-transcript text here>"
}
```

## Targets

- UK general ≤ 18% WER
- UK cardiology with prompt ≤ 14%
- EN general ≤ 10%
- EN cardiology with prompt ≤ 8%

Files are **not committed** until the clinical content lead approves them.
For local testing, generate synthetic audio via the team-internal TTS
pipeline (see `docs/onboarding.md § wer-fixtures`).
