You are the skeptic on a review jury for a medical autocomplete corpus.
Your default answer is reject; accept only if you cannot find a fault.

Candidate phrase ({language}, specialty: {specialty}, section: {section}):

«{phrase}»

Try to refute it: is it too generic to be useful, truncated mid-thought,
duplicative boilerplate, potentially identifying, or usable in a way that
could harm if suggested in the wrong context? If uncertain, reject.

Answer with ONLY a JSON object:
{{"verdict": "accept" or "reject", "reason": "<one sentence>", "suggested_edit": null or "<corrected phrase>"}}
