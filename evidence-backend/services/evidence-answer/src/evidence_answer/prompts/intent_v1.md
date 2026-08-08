---
name: intent
version: "1.0"
role: generator.fast
temperature_max: 0.0
---

<!--
Intent extraction. JSON-constrained decode: the gateway is handed the
ClinicalIntent JSON schema, so the model cannot emit prose. A malformed
result still gets one retry, then the pipeline falls back to a keyword plan
and records `intent_unparsed` in the trace.

QS1 note: this output is the ONLY thing that may reach a web query. Concepts
must be clinical terms — never names, dates, identifiers, or verbatim
sentences from the question.
-->

Extract the clinical intent of the question below. Output JSON only.

Rules:

- `question_type`: one of `diagnosis`, `therapy`, `dosing`, `interaction`,
  `contraindication`, `prognosis`, `etiology`, `prevention`, `other`.
- `concepts`: 1–8 clinical terms — conditions, drugs, procedures, findings.
  Each concept is a short term (1–4 words) in the language of the question.
  **Never** copy a whole sentence. **Never** include a person's name, a date,
  an age in the form of a birth date, a phone number, an identifier, or any
  other detail about a specific individual. Generalize: "65-year-old man with
  CKD" → concepts `["chronic kidney disease"]`, population `"elderly"`.
- `population`: a short qualifier if the question names one (`"pregnancy"`,
  `"children"`, `"CKD stage 4"`, `"elderly"`), otherwise null.
- `negations`: terms the question explicitly excludes ("without penicillin
  allergy" → `["penicillin allergy"]`), otherwise an empty list.

Question:

{question}
