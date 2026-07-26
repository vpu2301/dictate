"""Generate the two-speaker conversation fixture corpus (sprint 14, ADR-0034).

Synthesises doctor/patient dialogues with macOS TTS using a DISTINCT real
voice per speaker (Lesya uk_UA + Milena ru_RU reading the Ukrainian text;
Samantha en_US + Daniel en_GB for the English dialogue; Zosia pl_PL as the
third voice). Distinct TTS voices give stable, realistic speaker embeddings —
an earlier pitch-shifted-clone approach produced unstable embeddings on
sub-second utterances. Accent does not matter for diarization; voice
identity does (word-attribution scoring uses the generator's exact turn
boundaries, never ASR). Utterances are concatenated with deterministic
silences, so the reference speaker-turn boundaries are EXACT — the generator
writes them, nothing is hand-labeled after the fact.

The corpus is committed under eval/conversations/v1/ (same policy as
eval/corpus/v1: audio in-repo so the DER gate is reproducible without macOS).
Re-running this script regenerates it; `say` output is not bit-stable across
macOS versions, so regeneration implies re-baselining the DER numbers
(ADR-0034 covers re-baselining, mirroring ADR-0019 for WER).

Each dialogue dir:
    audio.wav        16 kHz mono s16 PCM (matches eval/corpus/v1 format)
    reference.json   exact turn boundaries + roles + text (ground truth)

Usage (macOS only; needs `say` + ffmpeg):
    uv run python scripts/eval/build_conversation_fixtures.py
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_ROOT = REPO_ROOT / "eval" / "conversations" / "v1"
SAMPLE_RATE = 16_000

# Voice per speaker slot, per language.
VOICES: dict[str, dict[str, tuple[str, float | None]]] = {
    # (voice, formant/pitch shift | None).
    # Measured ECAPA separability (2026-07-26, this corpus's sentences):
    # Lesya–Daria 0.42 — the doctor/patient pair. Zosia/Zuzana/Laura cannot
    # read Cyrillic (degenerate output). Raw Milena is a near-twin of Lesya
    # (0.54–0.70 per chunk) — a case cosine clustering fundamentally cannot
    # separate (ADR-0034 known limitation), so the third voice is Milena
    # shifted to a male-ish register: the fixture verifies that a DISTINCT
    # extra voice lands UNKNOWN. Shift artifacts are fine here: C's turns
    # are 2.5-3.7 s, where shifted-voice embeddings are stable.
    "uk": {"A": ("Lesya", None), "B": ("Daria", None), "C": ("Milena", 0.8)},
    "en": {"A": ("Samantha", None), "B": ("Daniel", None), "C": ("Albert", None)},
}


@dataclass(frozen=True)
class Utt:
    speaker: str  # "A" | "B" | "C"
    text: str
    pause_after_ms: int = 450  # silence inserted AFTER this utterance


@dataclass(frozen=True)
class Dialogue:
    dialogue_id: str
    language: str  # uk | en  (selects the VOICES row)
    # role mapping for the reference (who A/B/C actually are)
    roles: dict[str, str]
    utterances: list[Utt]
    notes: str = ""


# ── Corpus definition ─────────────────────────────────────────────────
# Clinical-style, PII-free (no names, no dates of birth, no addresses).

DIALOGUES: list[Dialogue] = [
    Dialogue(
        "uk-consult-001",
        "uk",
        {"A": "doctor", "B": "patient"},
        [
            Utt("A", "Доброго дня, проходьте, сідайте. Що вас турбує?"),
            Utt("B", "Доброго дня, лікарю. Вже тиждень болить голова, особливо зранку."),
            Utt("A", "Чи вимірювали ви артеріальний тиск останнім часом?"),
            Utt("B", "Так, вчора був сто сорок на дев'яносто."),
            Utt("A", "Чи приймаєте ви зараз якісь препарати від тиску?"),
            Utt("B", "Ні, нічого не приймаю, тільки іноді цитрамон від голови."),
            Utt("A", "Призначаю добове моніторування тиску та загальний аналіз крові."),
        ],
        notes="doctor opens; canonical mapping fixture",
    ),
    Dialogue(
        "uk-cardio-002",
        "uk",
        {"A": "doctor", "B": "patient"},
        [
            Utt("A", "На що скаржитеся сьогодні?"),
            Utt("B", "Серце ніби вискакує, і задишка коли підіймаюся сходами."),
            Utt("A", "Як давно з'явилася задишка при фізичному навантаженні?"),
            Utt("B", "Місяців зо два тому, спочатку не звертала уваги."),
            Utt("A", "Чи бували напади болю за грудиною з віддачею у ліву руку?"),
            Utt("B", "Один раз було, минулого тижня, хвилин п'ять тримало."),
            Utt("A", "Направляю вас на електрокардіограму та ехокардіографію серця."),
            Utt("B", "Добре, лікарю, а це терміново?"),
            Utt("A", "Бажано цього тижня. Також здайте аналіз на тропонін."),
        ],
        notes="cardiology vocabulary; doctor opens",
    ),
    Dialogue(
        "uk-patient-opens-003",
        "uk",
        {"A": "patient", "B": "doctor"},
        [
            Utt("A", "Лікарю, вибачте, я без запису, але мені дуже зле вже другий день."),
            Utt("B", "Сідайте, розповідайте. Яка у вас температура?"),
            Utt("A", "Тридцять вісім і п'ять, і горло болить, ковтати боляче."),
            Utt("B", "Відкрийте рот, скажіть а-а-а. Мигдалики збільшені, з нальотом."),
            Utt("A", "Це ангіна, так? У мене таке було торік."),
            Utt("B", "Схоже на гострий тонзиліт. Призначаю антибіотик та полоскання."),
        ],
        notes="PATIENT opens the session — opener heuristic must not blindly map first voice to doctor",
    ),
    Dialogue(
        "uk-command-004",
        "uk",
        {"A": "doctor", "B": "patient"},
        [
            Utt("A", "Розкажіть, будь ласка, як почалося захворювання."),
            Utt(
                "B",
                "Спочатку був просто кашель, а потім, новий абзац, так мій син каже "
                "постійно, вибачте, потім додалася температура.",
            ),
            Utt("A", "Зрозуміло. Чи був кашель сухий або з мокротинням?"),
            Utt("B", "Сухий, особливо вночі, аж спати не могла."),
        ],
        notes="patient says «новий абзац» mid-story — voice-command gating fixture",
    ),
    Dialogue(
        "uk-rapid-005",
        "uk",
        {"A": "doctor", "B": "patient"},
        [
            Utt("A", "Алергія на ліки є?", 300),
            Utt("B", "Ні.", 300),
            Utt("A", "Курите?", 300),
            Utt("B", "Ні, кинув.", 300),
            Utt("A", "Давно?", 300),
            Utt("B", "Три роки тому.", 300),
            Utt("A", "Алкоголь?", 300),
            Utt("B", "Рідко, по святах.", 300),
            Utt("A", "Тиск підвищується?", 300),
            Utt("B", "Буває, після стресу."),
        ],
        notes="rapid short turns — hardest windowed-diarization case",
    ),
    Dialogue(
        "uk-third-voice-006",
        "uk",
        {"A": "doctor", "B": "patient", "C": "other"},
        [
            Utt("A", "Проходьте. Що привело вас сьогодні?"),
            Utt("B", "Батько скаржиться на запаморочення, я його привела."),
            Utt("C", "Та нормально все зі мною, просто голова крутиться коли встаю."),
            Utt("A", "Чи падали ви при цих запамороченнях?"),
            Utt("C", "Один раз, тиждень тому, але не сильно."),
            Utt("B", "Він ще й їсти став менше, схуд за місяць."),
        ],
        notes="third voice present — pilot cap is 2 speakers, extra voice must land UNKNOWN, not crash",
    ),
    Dialogue(
        "uk-anamnesis-007",
        "uk",
        {"A": "doctor", "B": "patient"},
        [
            Utt("A", "Розкажіть про перенесені захворювання та операції."),
            Utt(
                "B",
                "У дитинстві хворіла на вітрянку і кір. У двадцять років видалили "
                "апендицит. П'ять років тому знайшли гіпотиреоз, приймаю левотироксин "
                "п'ятдесят мікрограмів щоранку. Мама хворіла на цукровий діабет "
                "другого типу, тато мав гіпертонію. Алергії на ліки не помічала, "
                "хіба що на цитрусові висип буває.",
                600,
            ),
            Utt("A", "Дякую, дуже докладно. Чи були переливання крові?"),
            Utt("B", "Ні, не було."),
        ],
        notes="long patient monologue (anamnesis) — mapping must survive doctor being the minority speaker",
    ),
    Dialogue(
        "en-consult-008",
        "en",
        {"A": "doctor", "B": "patient"},
        [
            Utt("A", "Good morning, please have a seat. What brings you in today?"),
            Utt("B", "I've had this dull pain in my lower back for about two weeks now."),
            Utt("A", "Does the pain radiate down either leg, or stay in one place?"),
            Utt("B", "It shoots down my right leg sometimes, especially when I bend over."),
            Utt("A", "Any numbness or tingling in the foot? Any trouble with bladder control?"),
            Utt("B", "Some tingling in the toes, but nothing else."),
            Utt("A", "I'll order an MRI of the lumbar spine and prescribe an anti-inflammatory."),
        ],
        notes="English coverage; doctor opens",
    ),
]


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(cmd)}\n{proc.stderr}")


def _tts_wav(text: str, voice: str, shift: float | None, tmp: Path, idx: int) -> Path:
    """One utterance -> 16 kHz mono s16 wav, optionally register-shifted."""
    aiff = tmp / f"u{idx}.aiff"
    wav = tmp / f"u{idx}.wav"
    _run(["say", "-v", voice, "-o", str(aiff), text])
    if shift is None:
        af = f"aresample={SAMPLE_RATE}"
    else:
        # asetrate re-pitches AND re-times; atempo undoes the timing change,
        # net effect: pitch + formant shift at the original speaking rate.
        af = (
            f"aresample={SAMPLE_RATE},asetrate={int(SAMPLE_RATE * shift)},"
            f"aresample={SAMPLE_RATE},atempo={1 / shift:.6f}"
        )
    _run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff),
         "-ac", "1", "-af", af, "-ar", str(SAMPLE_RATE),
         "-c:a", "pcm_s16le", str(wav)]
    )
    return wav


def _read_pcm(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1
        return w.readframes(w.getnframes())


def _trim_silence(pcm: bytes, threshold: int = 200) -> bytes:
    """Trim leading/trailing near-silence so reference boundaries are tight."""
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm)
    first, last = 0, n
    for i, s in enumerate(samples):
        if abs(s) > threshold:
            first = i
            break
    for i in range(n - 1, -1, -1):
        if abs(samples[i]) > threshold:
            last = i + 1
            break
    return pcm[first * 2 : last * 2]


def build_dialogue(dlg: Dialogue) -> dict:
    out_dir = OUT_ROOT / dlg.dialogue_id
    out_dir.mkdir(parents=True, exist_ok=True)
    lead_in_ms = 400
    cursor_ms = lead_in_ms
    turns = []
    pcm_parts: list[bytes] = [b"\x00\x00" * (SAMPLE_RATE * lead_in_ms // 1000)]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for idx, utt in enumerate(dlg.utterances):
            voice, shift = VOICES[dlg.language][utt.speaker]
            wav = _tts_wav(utt.text, voice, shift, tmp, idx)
            pcm = _trim_silence(_read_pcm(wav))
            dur_ms = len(pcm) // 2 * 1000 // SAMPLE_RATE
            turns.append(
                {
                    "speaker": utt.speaker,
                    "role": dlg.roles[utt.speaker],
                    "text": utt.text,
                    "start_ms": cursor_ms,
                    "end_ms": cursor_ms + dur_ms,
                }
            )
            pcm_parts.append(pcm)
            cursor_ms += dur_ms
            pause = utt.pause_after_ms
            pcm_parts.append(b"\x00\x00" * (SAMPLE_RATE * pause // 1000))
            cursor_ms += pause

    audio = b"".join(pcm_parts)
    with wave.open(str(out_dir / "audio.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio)

    reference = {
        "dialogue_id": dlg.dialogue_id,
        "language": dlg.language,
        "sample_rate": SAMPLE_RATE,
        "duration_ms": cursor_ms,
        "num_speakers": len({u.speaker for u in dlg.utterances}),
        "roles": dlg.roles,
        "notes": dlg.notes,
        "turns": turns,
    }
    (out_dir / "reference.json").write_text(
        json.dumps(reference, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return reference


def main() -> int:
    if sys.platform != "darwin" or not shutil.which("say"):
        raise SystemExit("fixture generation needs macOS `say` (corpus is committed; CI never runs this)")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg required")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    total_ms = 0
    for dlg in DIALOGUES:
        ref = build_dialogue(dlg)
        total_ms += ref["duration_ms"]
        print(f"  {dlg.dialogue_id}: {ref['duration_ms'] / 1000:.1f}s, {len(ref['turns'])} turns")
    print(f"corpus: {len(DIALOGUES)} dialogues, {total_ms / 1000:.1f}s total -> {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
