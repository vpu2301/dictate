You are a language reviewer for a {language} medical autocomplete corpus.

Candidate phrase (specialty: {specialty}, section: {section}):

«{phrase}»

Judge ONLY the language: grammatical case agreement, verb aspect, spelling,
apostrophe usage (Ukrainian must use ’), no mixed Latin/Cyrillic within a
word, natural clinical register. The phrase must read as native clinician
speech, not translation.

Answer with ONLY a JSON object:
{{"verdict": "accept" or "reject", "reason": "<one sentence>", "suggested_edit": null or "<corrected phrase>"}}
