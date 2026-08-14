# Per-section WER A/B fixtures (sprint 06 / refilled in sprint 21)

`run_per_section_wer.py` expects per-utterance JSON files here:

    {"audio": "<wav path>", "language": "uk", "section": "diagnosis",
     "reference": "<gold transcript>"}

This directory was referenced by `make wer-eval-per-section` since sprint 06
but never committed — the target exited on the missing-dir guard (found in
sprint-21 explore). It now exists as the drop target for the clinician
recordings that drive the sprint-21 ASR-prompt A/B gate
(`corpus-forge prompts --promote` consumes the two WER report JSONs).

Known caveat (docs/sprint-21/EXPLORE.md §4): run_per_section_wer.py has a
private tokenizer that differs from wer_lib's — compare its numbers only
against its own output, never against run_wer.py's.
