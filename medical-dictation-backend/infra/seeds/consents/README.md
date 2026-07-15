# Approved consent texts (S11 step 03)

One file per `(type, version)`: `<type>-<version>.md`. The file's
**exact bytes** are part of the legal record: a digital consent's
canonical document (see `core_service/domain/consent_canonical.py`)
embeds the sha256 of the text file, so the КЕП signature binds the
precise wording the patient consented to.

Rules:

- **Never edit a published file.** A wording change is a NEW version
  (`ai_scribe-v2.md`), captured on new consents; old envelopes keep
  verifying against the old bytes.
- The set of files here IS the approved set: a consent captured with
  `method='digital'` and a `(type, version)` that has no file is
  rejected with 422 `consent_text_version_unknown`.
- Texts require clinical/legal sign-off before pilot use — tracked in
  `todo.md` ("Consent text legal review"). Engineering owns the
  binding mechanism, not the wording.
- Deployment: the directory is baked/mounted read-only at the path in
  `MDX_CONSENT_TEXTS_DIR` (defaults to this repo path for local dev).
