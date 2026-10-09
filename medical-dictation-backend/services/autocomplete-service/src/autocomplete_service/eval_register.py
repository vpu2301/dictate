"""The data register — corpus-v3 Epic F.

WHAT IT IS. One page an auditor can read that answers: which datasets does
this system use to measure its own quality, where did each come from, whose
voices are in them and on what basis, which versions are frozen, and can I
have it as a document. Every one of those facts already existed — in a
snapshot row, an import journal, a git tag, somebody's memory — and
assembling them per question took half a day. This module assembles them
once, automatically, from the artefacts themselves.

WHAT IT IS NOT. A legal opinion. Nothing here classifies the system under
the EU AI Act, and the register says so on its own face. See
docs/compliance/data-register-ai-act.md.

THE TWO FIELDS PEOPLE GET WRONG, and they are adjacent on the page:

  contains_patient_data   always false, enforced by a CHECK constraint in
                          migration 0097. The eval corpus is scripted
                          synthetic speech and the policy is upheld at three
                          doors before this one.
  contains_personal_data  usually TRUE. The recordings are identifiable
                          employees reading a script, and a voice is
                          personal data under the GDPR whatever the words
                          are. Reading the first field and assuming the
                          second is the most common way a register like this
                          ends up being wrong.

AUTOFILL, NOT DATA ENTRY. A register somebody has to remember to update is a
register that is out of date the first busy week. Every published snapshot
and every committed CSV import writes its own entry, re-using the SHA-256
that was computed for integrity anyway — one digest per artefact, never two.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any, Final
from uuid import UUID

__all__ = [
    "PdfUnavailableError",
    "import_entry",
    "render_html",
    "render_pdf",
    "snapshot_entry",
]

#: Every corpus artefact this platform produces is scripted synthetic
#: speech. `derived` and `external` exist in the vocabulary for entries a
#: human adds later; nothing here can produce them.
SYNTHETIC: Final = "synthetic_scripted"

#: GDPR Art. 6(1)(a). The voices belong to employees who consented
#: specifically to this use, and the consent is revocable — which is what
#: corpus_speaker_consents records and what publish honours.
LEGAL_BASIS: Final = "GDPR Art. 6(1)(a) — згода спікера (corpus_voice), відкличувана"

#: Named rather than dated: the retention rule is "as long as the corpus is
#: the measurement baseline", and a date would be a guess.
RETENTION: Final = "поки корпус лишається базою виміру; перегляд щорічно"


def snapshot_entry(
    *,
    version: int,
    manifest_sha256: str,
    snapshot_id: UUID,
    utterances: int,
    speakers: list[UUID],
) -> dict[str, Any]:
    """A published snapshot's register entry.

    ``frozen=True`` unconditionally: a snapshot is immutable by construction
    (migration 0091), which is the whole reason a WER can be attributed to
    one. Recording it as a claim rather than a property would invite the
    question of who checked.
    """
    return {
        "name": "Клінічний корпус виміру WER",
        "version": f"snapshot-v{version}",
        "sha256": manifest_sha256,
        "purpose": (
            "Вимірювання якості розпізнавання мовлення (WER/CER, точність доз) "
            "на замороженому наборі сценарних реплік."
        ),
        "data_origin": SYNTHETIC,
        "contains_personal_data": bool(speakers),
        "speakers": speakers,
        "legal_basis": LEGAL_BASIS,
        "retention_period": RETENTION,
        "storage_location": "PostgreSQL (corpus_eval_snapshot_items, corpus_eval_takes)",
        "frozen": True,
        "source_kind": "corpus_snapshot",
        "source_id": snapshot_id,
        "utterances": utterances,
    }


def import_entry(
    *,
    filename: str,
    file_sha256: str,
    import_id: UUID,
    rows_added: int,
) -> dict[str, Any]:
    """A committed CSV import's register entry — text only, no audio.

    ``contains_personal_data=False`` is correct here and nowhere else in
    this module: an import is a spreadsheet of synthetic sentences. The
    voices enter later, when somebody records them, and that is the snapshot
    entry's business.
    """
    return {
        "name": f"Імпорт реплік: {filename}",
        "version": file_sha256[:12],
        "sha256": file_sha256,
        "purpose": "Сценарні репліки для запису вимірювального корпусу (текст без аудіо).",
        "data_origin": SYNTHETIC,
        "contains_personal_data": False,
        "speakers": [],
        "legal_basis": "Не персональні дані — синтетичні сценарні тексти.",
        "retention_period": RETENTION,
        "storage_location": "PostgreSQL (corpus_eval_script_items, corpus_eval_imports)",
        "frozen": True,
        "source_kind": "csv_import",
        "source_id": import_id,
        "utterances": rows_added,
    }


# ── the auditor's document ─────────────────────────────────────────────

_ORIGIN_LABEL: Final[dict[str, str]] = {
    "synthetic_scripted": "синтетичні сценарні",
    "derived": "похідні",
    "external": "зовнішні",
}

_STYLE: Final = """
  body { font-family: DejaVu Sans, Helvetica, sans-serif; font-size: 10pt;
         color: #14213d; margin: 24pt; }
  h1 { font-size: 16pt; margin: 0 0 4pt; }
  .sub { color: #5b6478; margin: 0 0 16pt; }
  .notice { border-left: 3pt solid #2a6f97; background: #eef4f8;
            padding: 8pt 10pt; margin: 0 0 16pt; }
  .caveat { border-left: 3pt solid #b8860b; background: #fdf6e3;
            padding: 8pt 10pt; margin: 16pt 0 0; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 18pt; }
  th, td { border: 0.5pt solid #c8d0dc; padding: 4pt 6pt;
           text-align: left; vertical-align: top; }
  th { background: #eef1f5; font-weight: 600; }
  code { font-family: DejaVu Sans Mono, monospace; font-size: 8.5pt; }
  .yes { color: #9b1c1c; font-weight: 600; }
  .no  { color: #1d6a3a; }
"""


def _flag(value: bool) -> str:
    return '<span class="yes">так</span>' if value else '<span class="no">ні</span>'


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def render_html(
    *,
    entries: list[dict[str, Any]],
    consents: list[dict[str, Any]],
    generated_at: datetime,
    tenant_id: UUID,
) -> str:
    """The register as a self-contained document.

    Also the PDF's source: one rendering, so the page an auditor is shown in
    the console and the file they take away cannot disagree.
    """
    rows = "\n".join(
        f"""<tr>
          <td>{_e(e["name"])}</td>
          <td>{_e(e["version"])}</td>
          <td><code>{_e(str(e["sha256"])[:16])}…</code></td>
          <td>{_e(e["purpose"])}</td>
          <td>{_e(_ORIGIN_LABEL.get(str(e["data_origin"]), e["data_origin"]))}</td>
          <td>{_flag(bool(e["contains_patient_data"]))}</td>
          <td>{_flag(bool(e["contains_personal_data"]))}</td>
          <td>{_e(e["retention_period"])}</td>
          <td>{"так" if e["frozen"] else "ні"}</td>
        </tr>"""
        for e in entries
    )
    consent_rows = "\n".join(
        f"""<tr>
          <td><code>{_e(c["speaker_id"])}</code></td>
          <td>{_e(c["scope"])}</td>
          <td>{_e(c["granted_at"])}</td>
          <td>{_e(c["revoked_at"]) or "—"}</td>
          <td>{"чинна" if c["revoked_at"] is None else "відкликана"}</td>
          <td>{_e(c.get("takes_recorded", 0))}</td>
        </tr>"""
        for c in consents
    )
    return f"""<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<title>Реєстр даних — Klarnote</title><style>{_STYLE}</style></head><body>
<h1>Реєстр даних</h1>
<p class="sub">Klarnote · тенант <code>{_e(tenant_id)}</code> ·
сформовано {_e(generated_at.isoformat(timespec="seconds"))}</p>

<div class="notice">
  <strong>Корпус — лише сценарні репліки.</strong> Дані пацієнтів заборонені
  політикою: запис можливий тільки за наперед відомим текстом, кожен запис
  проходить перевірку на персональні ідентифікатори, а разовий («ad-hoc»)
  запис вимагає іменного підтвердження про відсутність даних пацієнта.
  Поле «дані пацієнтів» у таблиці нижче заборонено бути «так» на рівні схеми
  бази даних.
</div>

<h2>Набори даних</h2>
<table>
  <thead><tr>
    <th>Назва</th><th>Версія</th><th>SHA-256</th><th>Призначення</th>
    <th>Походження</th><th>Дані пацієнтів</th><th>Персональні дані</th>
    <th>Зберігання</th><th>Заморожено</th>
  </tr></thead>
  <tbody>{rows or '<tr><td colspan="9">Записів немає.</td></tr>'}</tbody>
</table>

<h2>Спікери та згоди</h2>
<p class="sub">Голос — персональні дані незалежно від змісту тексту.
Відкликання згоди виключає дублі спікера з майбутніх вимірів; уже
опубліковані знімки лишаються незмінними, бо вони — журнал того, що було
виміряно.</p>
<table>
  <thead><tr>
    <th>Спікер</th><th>Обсяг</th><th>Надано</th><th>Відкликано</th>
    <th>Статус</th><th>Дублів записано</th>
  </tr></thead>
  <tbody>{consent_rows or '<tr><td colspan="6">Згод не зафіксовано.</td></tr>'}</tbody>
</table>

<h2>Ведення записів</h2>
<p>Журнали run-ів, імпортів і спроб запису (<code>corpus_eval_runs</code>,
<code>corpus_eval_imports</code>, <code>corpus_eval_take_attempts</code>,
<code>corpus_eval_gold_revisions</code>) є механізмом record-keeping і тут
не дублюються — реєстр лише посилається на них.</p>

<div class="caveat">
  <strong>Застереження.</strong> Реєстр — інженерний інструмент прозорості,
  а не юридичний висновок. Класифікацію системи за EU AI Act (зокрема чи
  підпадає Klarnote під високоризикові через медичний контекст) має
  підтвердити юрист — див. розділ «Класифікація» у
  <code>docs/compliance/data-register-ai-act.md</code>.
</div>
</body></html>"""


class PdfUnavailableError(RuntimeError):
    """The PDF renderer's native libraries are not installed here.

    Raised rather than silently returning HTML with a PDF content type: a
    file an auditor cannot open is worse than a clear "this deployment
    cannot render PDFs", and the JSON and HTML exports carry the same
    content.
    """


def render_pdf(document: str) -> bytes:
    """The same document as a PDF.

    weasyprint is imported lazily and declared as the ``pdf`` extra, exactly
    as ``medical_kep`` does for report rendering: it needs pango/cairo at
    runtime, and neither the import path nor a dev laptop should have to
    carry that for a surface almost nobody hits.
    """
    try:
        from weasyprint import HTML  # noqa: PLC0415
    except Exception as exc:  # ImportError, or a missing native library
        raise PdfUnavailableError(str(exc)) from exc
    return bytes(HTML(string=document).write_pdf())
