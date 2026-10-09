"""Building the eval/corpus/v1 artefact: per-utterance files, manifest, ZIP.

One module because three callers must produce byte-identical output or the
whole integrity story collapses:

  · the live export (every take as it stands right now),
  · publishing (freezing a snapshot, and hashing what was frozen),
  · a snapshot's export (re-materialising exactly what was published).

If publishing hashed one rendering of metadata.json and the export wrote
another, ``build_corpus_manifest.py --check`` would fail on a corpus that
was never actually tampered with, and the operator would learn to ignore
it. So the rendering lives here, once.

Layout mirrors eval/corpus/v1/README.md: ``<utterance_id>/`` for baseline
lines, ``subsets/<subset>/<utterance_id>/`` for the adversarial ones, each
holding audio.wav + transcript.txt + metadata.json.

PAIRED REPLICAS (corpus-v3 Epic D) are the one exception, and the exception
is deliberately narrow. A paired line is recorded in BOTH conditions, so it
yields two utterances under one script_id — which the flat layout cannot
hold, since the second would overwrite the first in the ZIP and collide in
the manifest. Those, and only those, get the condition appended to the
directory name (``uk-num-010--phone-speaker-distance/``). Every unpaired
utterance keeps the exact path it had, because the stored manifest digests
of existing snapshots are taken over these strings.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from typing import Any

from .eval_script import DICTATION_SOURCE

MANIFEST_FILES = ("audio.wav", "transcript.txt", "metadata.json")


@dataclass(frozen=True, slots=True)
class Utterance:
    script_id: str
    language: str
    specialty: str
    subset: str | None
    transcript: str
    condition: str
    duration_ms: int
    source: str  # builtin | authored | adhoc
    audio: bytes | None  # None when a snapshot's take has since been deleted
    # Part of the paired design (Epic D). ONLY these disambiguate their
    # directory by condition — see the module header on why the narrowness
    # matters.
    paired: bool = False


@dataclass(frozen=True, slots=True)
class Rendered:
    path: str
    files: dict[str, bytes]
    manifest_entry: dict[str, Any]


def corpus_path(
    script_id: str, subset: str | None, *, condition: str | None = None
) -> str:
    """``condition`` is passed only for paired utterances; passing it for
    anything else would silently change the path of an already-published
    snapshot and break its manifest digest."""
    leaf = f"{script_id}--{condition}" if condition else script_id
    return f"subsets/{subset}/{leaf}" if subset else leaf


def render(u: Utterance) -> Rendered:
    """One utterance as the three files the corpus format defines.

    ``dictation_source`` stays inside the README's closed vocabulary
    (anonymized_real / authored_by_linguist / authored_by_clinician) — every
    take here is a clinician reading synthetic text, so it is always
    ``authored_by_clinician``. The finer distinction goes in ``capture``, an
    additive key the manifest builder ignores: 'scripted' means the words
    existed before the recording, 'adhoc' means they were written down
    afterwards from what was said. A corpus reader must be able to tell
    those apart — the second is a weaker guarantee about the gold text.
    """
    audio = u.audio or b""
    transcript = f"{u.transcript}\n"
    metadata: dict[str, Any] = {
        "utterance_id": u.script_id,
        "language": u.language,
        "specialty": u.specialty,
        "duration_s": round(u.duration_ms / 1000, 1),
        "dictation_source": DICTATION_SOURCE,
    }
    if u.subset:
        metadata["subset"] = u.subset
    metadata["condition"] = u.condition
    if u.paired:
        metadata["paired"] = True
    if u.source == "adhoc":
        metadata["capture"] = "adhoc"
    metadata_text = f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n"

    files = {
        "audio.wav": audio,
        "transcript.txt": transcript.encode(),
        "metadata.json": metadata_text.encode(),
    }
    path = corpus_path(
        u.script_id, u.subset, condition=u.condition if u.paired else None
    )
    entry: dict[str, Any] = {
        "utterance_id": u.script_id,
        "language": u.language,
        "specialty": u.specialty,
        "duration_s": metadata["duration_s"],
        "dictation_source": DICTATION_SOURCE,
    }
    if u.subset:
        entry["subset"] = u.subset
    if u.paired:
        # Two entries share a utterance_id; the manifest has to say which
        # recording each one is, or a reader cannot pair them back up.
        entry["paired"] = True
        entry["condition"] = u.condition
    entry["path"] = path
    entry["sha256"] = {
        name: hashlib.sha256(files[name]).hexdigest() for name in MANIFEST_FILES
    }
    return Rendered(path=path, files=files, manifest_entry=entry)


def manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """The canonical serialisation a manifest's SHA-256 is taken over.

    sort_keys is deliberately OFF: the entry order above is the documented
    field order of the corpus format, and re-ordering it would make the
    stored digest disagree with the file the export writes.
    """
    return f"{json.dumps(manifest, ensure_ascii=False, indent=2)}\n".encode()


def build_manifest(
    entries: list[dict[str, Any]], *, snapshot_version: int | None = None
) -> dict[str, Any]:
    manifest: dict[str, Any] = {"schema_version": 1, "corpus_version": "v1"}
    if snapshot_version is not None:
        manifest["snapshot_version"] = snapshot_version
    manifest["utterances"] = entries
    return manifest


def build_zip(
    utterances: list[Utterance],
    *,
    snapshot_version: int | None = None,
    readme: str,
) -> tuple[bytes, dict[str, Any]]:
    """The archive plus the manifest it contains.

    Deterministic: fixed timestamps and fixed member order, so downloading
    the same snapshot twice yields the same bytes. An archive whose digest
    changes on every download cannot be checked by anyone.
    """
    entries: list[dict[str, Any]] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for u in sorted(utterances, key=lambda x: x.script_id):
            r = render(u)
            entries.append(r.manifest_entry)
            for name in MANIFEST_FILES:
                _write(zf, f"{r.path}/{name}", r.files[name])
        manifest = build_manifest(entries, snapshot_version=snapshot_version)
        _write(zf, "manifest-fragment.json", manifest_bytes(manifest))
        _write(zf, "README-UNPACK.txt", readme.encode())
    return buf.getvalue(), manifest


def _write(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    # SOURCE_DATE_EPOCH-style fixed stamp (1980-01-01 is the ZIP epoch);
    # zipfile refuses anything earlier.
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)
