# Gold-transcript style guide

Applies to every reference transcript in every corpus version — the vendored
script (`eval_script.py`), lines authored in the console, and rows imported
from CSV. Enforced advisorily by `eval_goldlint` at each of those three
doors, and swept over the existing corpus by `GET /corpus/eval/gold-lint`.

## The rule

**A gold transcript is written in spoken form.**

- Numbers as words: `сто сорок на дев'яносто`, not `140/90`.
- No digits, no `/`, no `%`.
- Units as they are said: `міліметрів ртутного стовпа`, `за хвилину`,
  `міліграмів`.
- Abbreviations the way a clinician pronounces them: `А Те`, `Че Ес Ес`,
  `Це Ер Бе` — not `АТ`, `ЧСС`, `СРБ`.
- One utterance per line, single spaces, no leading or trailing whitespace.

Names are not measurements and keep their digits: `HbA1c`, `B12`,
`COVID-19`. The linter notices them and has nothing to propose, which is how
you can tell the two cases apart.

## Why

The v2 measurement read **18.1% raw / 14.4% normalised**. Most of that
6.8-point gap was not the model mishearing anything — it was the reference
disagreeing with the model about house style. Two of the worst-scoring
replicas, `uk-cardiology-a001` ("пульс 68/хв") and `uk-drugs-001` ("5 мг
вранці"), sat at 50% WER on utterances the ASR got right, because the gold
was written in digits and the hypothesis in words.

The normaliser (`eval_normalize`, rules in
`services/autocomplete-service/src/autocomplete_service/data/eval_normalization_v2.yaml`)
folds those variants together, and the normalised score is the honest one.
It is not enough on its own:

- **Raw WER stays in the report** because normalised WER is the score that
  can be tuned into meaninglessness by adding rules. Two numbers side by
  side are a check on each other; one number is a number you have to trust.
- A gold written carelessly inflates raw WER for a reason that has nothing
  to do with recognition, and then every raw figure has to be read with a
  mental asterisk nobody remembers a month later.

So: **the normaliser is a safety net, not a licence for sloppy references.**

## What this changed

Corpus-v3 Epic B rewrote 14 vendored golds into spoken form. Ten of them
(numbers, doses, drug names) are pure formatting — the normalised score does
not move, because the normaliser was already folding both forms onto one
canonical string.

Four of them are the abbreviation lines, and those **change what the subset
measures**:

| before | after | the question it now asks |
| --- | --- | --- |
| `АТ стабільний` | `А Те стабільний` | does the ASR hear the letter names? |

The old gold measured two things at once — whether the ASR heard the letters
*and* whether the pipeline folded them into the written abbreviation — and
reported the sum as one WER. The new one measures recognition. Expansion is
an NLP behaviour and belongs to an NLP test, not to a word-error rate.

Migration `0093` journals all 14 revisions in `corpus_eval_gold_revisions`
with `canonical_equal` recording which kind each was, and any run that
started before a revision is marked **«еталон змінено»** when it is read
back. The mark is derived, not stored: a stored score is a fact about a
comparison that happened, and rewriting it later to note that the reference
moved would be editing the history of an edit.

## Changing a gold transcript later

- **dev set** — an ordinary edit. It is journalled.
- **test set** — the frozen holdout. `POST /corpus/eval/gold-lint/apply`
  refuses it unless the request carries `confirm_test_set: true`, and the
  audit event is raised to `warn` when the revision changes what is
  measured. Changing the holdout changes the measurement; that has to be a
  decision somebody made on purpose, not a side effect of tidying.
- **vendored spine** (`eval_script.py`) — a repository commit, reviewed like
  code. The sweep reports those lines and refuses to apply, and migration
  0093 is where the corresponding journal rows are seeded so that tenants'
  historical runs are marked the same way.

## Fixing a line

`GET /corpus/eval/gold-lint` returns every offending line with a proposed
rewrite produced by `eval_spoken` — the same rules file read backwards.
Every proposal is verified before it is shown: it is normalised again and
compared against the original's canonical form *and* numeric signature. A
proposal that moved a dose is discarded rather than displayed, which is why
some findings arrive with `suggestion: null`. Those need a human.

```
пульс 68/хв                     → пульс шістдесят вісім за хвилину
Глікований гемоглобін 7,2 %.    → Глікований гемоглобін сім цілих дві десятих відсотка.
HbA1c 6,9                       → HbA1c шість цілих дев'ять десятих
амоксицилін/клавуланова 875/125 → ... вісімсот сімдесят п'ять на сто двадцять п'ять
                                   (the slash between two WORDS is left for you)
```

## Related

- `eval/corpus/v1/README.md` — layout, subsets, provenance of the v1 set.
- `docs/eval/wer-methodology.md` — how the numbers are computed and reported.
- `services/.../data/eval_normalization_v2.yaml` — the rules, both directions.
