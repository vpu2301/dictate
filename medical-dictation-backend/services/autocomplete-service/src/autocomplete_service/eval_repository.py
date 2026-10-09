"""SQL for corpus_eval_takes (migration 0089).

Runs under tenant_connection as app_role — the table is plain tenant-scoped
(unlike the global corpus_candidates rows), so the RLS policy does all the
scoping and no SECURITY DEFINER path exists here.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import asyncpg

# Everything a listing needs; never the audio itself (a list of takes must
# not weigh megabytes).
_TAKE_COLS = """
    id, script_id, script_version, recorded_by, language, specialty, subset,
    condition, condition_confirmed, duration_ms, audio_sha256,
    octet_length(audio_wav) AS size_bytes, flagged_bad, flagged_note,
    flagged_by, flagged_at, created_at, updated_at
"""


async def list_takes(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"SELECT {_TAKE_COLS} FROM corpus_eval_takes ORDER BY script_id"
        )
    )


async def upsert_take(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    script_id: str,
    script_version: str,
    recorded_by: UUID,
    language: str,
    specialty: str,
    subset: str | None,
    say: str,
    transcript: str,
    condition: str,
    duration_ms: int,
    sample_rate: int,
    audio_sha256: str,
    audio_wav: bytes,
    condition_confirmed: bool = True,
) -> asyncpg.Record:
    """One take per (tenant, script line, CONDITION) since 0095: a paired
    replica holds one recording per condition, and re-recording replaces
    only the take for that condition — audio, attestation and snapshot text
    together.

    THE UPSERT CLEARS THE MANUAL RETAKE FLAG (0096). A new recording is a
    new take; inheriting "брак" from the one it replaced would leave the
    line in the retake queue forever and make the queue useless. Every other
    retake signal is derived from evidence newer than the audio, so they
    clear themselves — this is the one that has to be said explicitly."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO corpus_eval_takes
            (tenant_id, script_id, script_version, recorded_by, language,
             specialty, subset, say, transcript, condition, duration_ms,
             sample_rate, audio_sha256, audio_wav, condition_confirmed)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        ON CONFLICT ON CONSTRAINT eval_take_one_per_line_condition DO UPDATE SET
            script_version = EXCLUDED.script_version,
            recorded_by    = EXCLUDED.recorded_by,
            language       = EXCLUDED.language,
            specialty      = EXCLUDED.specialty,
            subset         = EXCLUDED.subset,
            say            = EXCLUDED.say,
            transcript     = EXCLUDED.transcript,
            condition      = EXCLUDED.condition,
            duration_ms    = EXCLUDED.duration_ms,
            sample_rate    = EXCLUDED.sample_rate,
            audio_sha256   = EXCLUDED.audio_sha256,
            audio_wav      = EXCLUDED.audio_wav,
            condition_confirmed = EXCLUDED.condition_confirmed,
            flagged_bad    = false,
            flagged_note   = NULL,
            flagged_by     = NULL,
            flagged_at     = NULL,
            updated_at     = now()
        RETURNING {_TAKE_COLS}
        """,
        tenant_id,
        script_id,
        script_version,
        recorded_by,
        language,
        specialty,
        subset,
        say,
        transcript,
        condition,
        duration_ms,
        sample_rate,
        audio_sha256,
        audio_wav,
        condition_confirmed,
    )
    assert row is not None  # RETURNING on a successful upsert always yields one row
    return row


async def get_take_audio(
    conn: asyncpg.Connection, script_id: str, condition: str | None = None
) -> asyncpg.Record | None:
    """``condition=None`` means "whatever this line has", which is the right
    answer for an unpaired replica and an arbitrary one for a paired replica
    — so the paired callers pass it."""
    return await conn.fetchrow(
        """
        SELECT audio_wav, audio_sha256, condition FROM corpus_eval_takes
         WHERE script_id = $1 AND ($2::text IS NULL OR condition = $2)
         ORDER BY updated_at DESC LIMIT 1
        """,
        script_id,
        condition,
    )


async def delete_take(
    conn: asyncpg.Connection, script_id: str, condition: str | None = None
) -> bool:
    """``condition=None`` deletes every recording of the line — which is what
    deleting the LINE means, and why delete_script_item passes nothing."""
    tag = await conn.execute(
        """
        DELETE FROM corpus_eval_takes
         WHERE script_id = $1 AND ($2::text IS NULL OR condition = $2)
        """,
        script_id,
        condition,
    )
    return not tag.endswith(" 0")


async def flag_take(
    conn: asyncpg.Connection,
    *,
    script_id: str,
    condition: str,
    flagged: bool,
    note: str | None,
    flagged_by: UUID,
) -> asyncpg.Record | None:
    """The human's "брак" mark (0096) — the one retake signal nothing can
    rederive, because it lives in whoever listened back."""
    return await conn.fetchrow(
        f"""
        UPDATE corpus_eval_takes
           SET flagged_bad  = $3,
               flagged_note = CASE WHEN $3 THEN $4 ELSE NULL END,
               flagged_by   = CASE WHEN $3 THEN $5::uuid ELSE NULL END,
               flagged_at   = CASE WHEN $3 THEN now() ELSE NULL END
         WHERE script_id = $1 AND condition = $2
        RETURNING {_TAKE_COLS}
        """,
        script_id,
        condition,
        flagged,
        note,
        flagged_by,
    )


async def fetch_for_export(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Everything the archive needs, audio included — export only.

    The LEFT JOIN supplies ``source``: a take whose line is not in the
    authored table came from the vendored script, and the corpus metadata
    has to be able to say which.
    """
    return list(
        await conn.fetch(
            """
            SELECT t.script_id, t.script_version, t.language, t.specialty,
                   t.subset, t.say, t.transcript, t.condition, t.duration_ms,
                   t.audio_sha256, t.audio_wav,
                   COALESCE(i.source, 'builtin') AS source,
                   COALESCE(i.paired, false) AS paired
            FROM corpus_eval_takes t
            LEFT JOIN corpus_eval_script_items i ON i.script_id = t.script_id
            ORDER BY t.script_id, t.condition
            """
        )
    )


async def fetch_for_publish(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """The same set, plus the take id a snapshot item must point at.

    ``excluded_by_consent`` (0097, Epic F) is the speaker's withdrawal
    carried through to the measurement: a take whose recordist has no active
    ``corpus_voice`` consent must not enter a NEW snapshot. It is computed
    here rather than filtered here so the publish route can say how many it
    dropped — a snapshot that silently shrank is worse than one that
    explains itself.
    """
    return list(
        await conn.fetch(
            """
            SELECT t.id AS take_id, t.script_id, t.language, t.specialty,
                   t.subset, t.say, t.transcript, t.condition, t.duration_ms,
                   t.audio_sha256, t.audio_wav, t.recorded_by,
                   COALESCE(i.source, 'builtin') AS source,
                   -- A vendored line belongs to the v1 corpus, which 0092
                   -- freezes as the holdout: 'test' is its only honest set.
                   COALESCE(i.dataset, 'test') AS dataset,
                   COALESCE(i.paired, false) AS paired,
                   NOT EXISTS (
                       SELECT 1 FROM corpus_speaker_consents c
                        WHERE c.speaker_id = t.recorded_by
                          AND c.scope = 'corpus_voice'
                          AND c.revoked_at IS NULL
                   ) AS excluded_by_consent
            FROM corpus_eval_takes t
            LEFT JOIN corpus_eval_script_items i ON i.script_id = t.script_id
            ORDER BY t.script_id, t.condition
            """
        )
    )


async def has_active_consent(
    conn: asyncpg.Connection, *, speaker_id: UUID, scope: str = "corpus_voice"
) -> bool:
    """Epic F: a take cannot be stored without a live consent from its
    speaker. Checked at the upload boundary, not only at publish, so the
    audio never lands in the first place."""
    row = await conn.fetchrow(
        """
        SELECT 1 FROM corpus_speaker_consents
         WHERE speaker_id = $1 AND scope = $2 AND revoked_at IS NULL
         LIMIT 1
        """,
        speaker_id,
        scope,
    )
    return row is not None


# ══ authored script lines (migration 0091) ═════════════════════════════
#
# The vendored script in eval_script.py stays the read-only spine; these are
# the lines a tenant added from the console. Both halves are served by
# GET /corpus/eval/script and both are legal upload targets — what differs is
# that only these can be edited or removed.

_ITEM_COLS = """
    id, script_id, language, specialty, subset, say, transcript, condition,
    source, dataset, paired, created_by, created_at, updated_at
"""


async def list_script_items(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"SELECT {_ITEM_COLS} FROM corpus_eval_script_items ORDER BY created_at"
        )
    )


async def get_script_item(
    conn: asyncpg.Connection, script_id: str
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_ITEM_COLS} FROM corpus_eval_script_items WHERE script_id = $1",
        script_id,
    )


async def insert_script_item(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    script_id: str,
    language: str,
    specialty: str,
    subset: str | None,
    say: str,
    transcript: str,
    condition: str | None,
    source: str,
    created_by: UUID,
    dataset: str = "dev",
    paired: bool = False,
) -> asyncpg.Record | None:
    """Returns None on a script_id collision — the caller retries with the
    next suffix rather than surfacing a 500 for a race two colleagues can
    lose against each other in the same second."""
    return await conn.fetchrow(
        f"""
        INSERT INTO corpus_eval_script_items
            (tenant_id, script_id, language, specialty, subset, say,
             transcript, condition, source, created_by, dataset, paired)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT ON CONSTRAINT eval_item_one_per_script_id DO NOTHING
        RETURNING {_ITEM_COLS}
        """,
        tenant_id,
        script_id,
        language,
        specialty,
        subset,
        say,
        transcript,
        condition,
        source,
        created_by,
        dataset,
        paired,
    )


async def insert_script_item_with_id(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    script_id: str,
    language: str,
    specialty: str,
    subset: str | None,
    say: str,
    transcript: str,
    condition: str | None,
    source: str,
    created_by: UUID,
    dataset: str,
    paired: bool = False,
) -> asyncpg.Record | None:
    """The CSV import's insert: the id comes from the FILE, not the allocator.

    §6 makes the id the import's identity key — that is what makes
    re-importing the same file a no-op instead of a duplicate set of lines
    under freshly-allocated ids. Same conflict behaviour as the allocator
    path (None, not an exception) so a row already present is reported as
    skipped rather than failing the whole file.
    """
    return await conn.fetchrow(
        f"""
        INSERT INTO corpus_eval_script_items
            (tenant_id, script_id, language, specialty, subset, say,
             transcript, condition, source, created_by, dataset, paired)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT ON CONSTRAINT eval_item_one_per_script_id DO NOTHING
        RETURNING {_ITEM_COLS}
        """,
        tenant_id,
        script_id,
        language,
        specialty,
        subset,
        say,
        transcript,
        condition,
        source,
        created_by,
        dataset,
        paired,
    )


async def existing_script_ids(
    conn: asyncpg.Connection, script_ids: list[str]
) -> set[str]:
    """Which of these ids the tenant already has — the import's dry-run input."""
    if not script_ids:
        return set()
    rows = await conn.fetch(
        "SELECT script_id FROM corpus_eval_script_items WHERE script_id = ANY($1::text[])",
        script_ids,
    )
    return {r["script_id"] for r in rows}


async def coverage_by_category(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Post-import coverage matrix: utterances per (dataset, language, subset).

    §6 asks for it after every import, and §1.2's whole point is that a
    corpus number is only readable next to the sample size behind it.
    """
    return list(
        await conn.fetch(
            """
            SELECT dataset, language, COALESCE(subset, 'baseline') AS subset,
                   count(*) AS utterances
            FROM corpus_eval_script_items
            GROUP BY dataset, language, COALESCE(subset, 'baseline')
            ORDER BY dataset, language, subset
            """
        )
    )


async def update_script_item(
    conn: asyncpg.Connection,
    *,
    script_id: str,
    language: str,
    specialty: str,
    subset: str | None,
    say: str,
    transcript: str,
    condition: str | None,
    dataset: str | None = None,
    paired: bool | None = None,
) -> asyncpg.Record | None:
    """``dataset=None`` leaves the set alone — moving a line between dev and
    test is a separate, deliberate act (§1.2 holdout discipline), not
    something an ordinary text edit gets to do by omission."""
    return await conn.fetchrow(
        f"""
        UPDATE corpus_eval_script_items
           SET language = $2, specialty = $3, subset = $4, say = $5,
               transcript = $6, condition = $7,
               dataset = COALESCE($8, dataset),
               paired = COALESCE($9, paired), updated_at = now()
         WHERE script_id = $1
        RETURNING {_ITEM_COLS}
        """,
        script_id,
        language,
        specialty,
        subset,
        say,
        transcript,
        condition,
        dataset,
        paired,
    )


async def delete_script_item(conn: asyncpg.Connection, script_id: str) -> bool:
    tag = await conn.execute(
        "DELETE FROM corpus_eval_script_items WHERE script_id = $1", script_id
    )
    return tag.endswith("1")


async def taken_script_ids(conn: asyncpg.Connection, prefix: str) -> set[str]:
    """Authored ids already in use under a generated prefix — the id
    allocator's input. Takes are keyed by script_id too, but a take cannot
    exist without its line, so the items table is the whole population."""
    rows = await conn.fetch(
        "SELECT script_id FROM corpus_eval_script_items WHERE script_id LIKE $1",
        f"{prefix}%",
    )
    return {r["script_id"] for r in rows}


# ══ snapshots (migration 0091) ═════════════════════════════════════════

_SNAPSHOT_COLS = """
    id, version, utterance_count, total_duration_ms, manifest_sha256,
    published_by, created_at
"""


async def next_snapshot_version(conn: asyncpg.Connection) -> int:
    row = await conn.fetchrow(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM corpus_eval_snapshots"
    )
    return int(row["v"]) if row else 1


async def insert_snapshot(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    version: int,
    utterance_count: int,
    total_duration_ms: int,
    manifest: str,
    manifest_sha256: str,
    published_by: UUID,
) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"""
        INSERT INTO corpus_eval_snapshots
            (tenant_id, version, utterance_count, total_duration_ms,
             manifest, manifest_sha256, published_by)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
        RETURNING {_SNAPSHOT_COLS}
        """,
        tenant_id,
        version,
        utterance_count,
        total_duration_ms,
        manifest,
        manifest_sha256,
        published_by,
    )
    assert row is not None
    return row


async def insert_snapshot_items(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    snapshot_id: UUID,
    items: list[dict[str, object]],
) -> None:
    await conn.executemany(
        """
        INSERT INTO corpus_eval_snapshot_items
            (tenant_id, snapshot_id, script_id, language, specialty, subset,
             transcript, condition, duration_ms, audio_sha256, take_id, source,
             dataset, paired)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        """,
        [
            (
                tenant_id,
                snapshot_id,
                i["script_id"],
                i["language"],
                i["specialty"],
                i["subset"],
                i["transcript"],
                i["condition"],
                i["duration_ms"],
                i["audio_sha256"],
                i["take_id"],
                i["source"],
                i["dataset"],
                i["paired"],
            )
            for i in items
        ],
    )


async def list_snapshots(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"SELECT {_SNAPSHOT_COLS} FROM corpus_eval_snapshots ORDER BY version DESC"
        )
    )


async def get_snapshot(
    conn: asyncpg.Connection, snapshot_id: UUID
) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"SELECT {_SNAPSHOT_COLS} FROM corpus_eval_snapshots WHERE id = $1",
        snapshot_id,
    )


async def list_snapshot_items(
    conn: asyncpg.Connection, snapshot_id: UUID
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT script_id, language, specialty, subset, transcript,
                   condition, duration_ms, audio_sha256, take_id, source, dataset
            FROM corpus_eval_snapshot_items
            WHERE snapshot_id = $1
            ORDER BY script_id
            """,
            snapshot_id,
        )
    )


async def snapshot_dataset_counts(
    conn: asyncpg.Connection, snapshot_id: UUID
) -> dict[str, int]:
    """How many utterances of each set a snapshot holds — what the console
    needs to say "score dev (86) / score test (36)" before a run exists."""
    rows = await conn.fetch(
        """
        SELECT dataset, count(*) AS n
        FROM corpus_eval_snapshot_items
        WHERE snapshot_id = $1
        GROUP BY dataset
        """,
        snapshot_id,
    )
    return {str(r["dataset"]): int(r["n"]) for r in rows}


async def fetch_snapshot_export(
    conn: asyncpg.Connection, snapshot_id: UUID
) -> list[asyncpg.Record]:
    """A published snapshot's utterances WITH their audio.

    The join is on take id, and ``audio_drifted`` says whether the bytes
    still hash to what was published: a take re-recorded after publication
    is a different recording, and an archive that quietly swapped it in
    would no longer be the thing the run scored. A missing take LEFT JOINs
    to NULL rather than vanishing — the export must be able to say "one
    utterance of this snapshot no longer has audio".
    """
    return list(
        await conn.fetch(
            """
            SELECT si.script_id, si.language, si.specialty, si.subset,
                   si.transcript, si.condition, si.duration_ms,
                   si.audio_sha256, si.source, si.paired,
                   t.audio_wav,
                   (t.audio_sha256 IS DISTINCT FROM si.audio_sha256) AS audio_drifted
            FROM corpus_eval_snapshot_items si
            LEFT JOIN corpus_eval_takes t ON t.id = si.take_id
            WHERE si.snapshot_id = $1
            ORDER BY si.script_id, si.condition
            """,
            snapshot_id,
        )
    )


async def snapshot_audio(
    conn: asyncpg.Connection, *, snapshot_id: UUID, script_id: str, condition: str
) -> asyncpg.Record | None:
    """One published RECORDING's audio — what the scorer sends to ASR.

    Keyed by condition since 0095: a paired replica has two recordings under
    one script_id, and fetching "the" audio for it would score one condition
    twice and never score the other.
    """
    return await conn.fetchrow(
        """
        SELECT si.script_id, si.language, si.specialty, si.transcript,
               si.audio_sha256, si.duration_ms, si.condition, si.paired,
               t.audio_wav
        FROM corpus_eval_snapshot_items si
        LEFT JOIN corpus_eval_takes t ON t.id = si.take_id
        WHERE si.snapshot_id = $1 AND si.script_id = $2 AND si.condition = $3
        """,
        snapshot_id,
        script_id,
        condition,
    )


# ══ scoring runs (migration 0091) ══════════════════════════════════════

_RUN_COLS = """
    id, snapshot_id, status, model, started_by, started_at, finished_at, summary,
    dataset, normalizer_version, corpus_sha256, engine, bootstrap_seed
"""

_RUN_ITEM_COLS = """
    script_id, condition, status, asr_job_id, hypothesis, wer, cer, ref_words, error,
    wer_norm, cer_norm, ref_words_norm, ref_chars_norm, dose_tokens, dose_exact,
    flags, speech_ms, updated_at
"""


async def insert_run(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    snapshot_id: UUID,
    model: str,
    started_by: UUID,
    dataset: str,
    normalizer_version: str,
    corpus_sha256: str,
    bootstrap_seed: int,
) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"""
        INSERT INTO corpus_eval_runs
            (tenant_id, snapshot_id, model, started_by, dataset,
             normalizer_version, corpus_sha256, bootstrap_seed)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING {_RUN_COLS}
        """,
        tenant_id,
        snapshot_id,
        model,
        started_by,
        dataset,
        normalizer_version,
        corpus_sha256,
        bootstrap_seed,
    )
    assert row is not None
    return row


async def merge_run_engine(
    conn: asyncpg.Connection, *, run_id: UUID, engine: str
) -> None:
    """Fold what asr-service reported into the run's engine record.

    Merged rather than overwritten: the facts arrive one completed job at a
    time (beam size from the first, a second language hint from the tenth),
    and a last-writer-wins column would end up describing the last utterance
    instead of the run.
    """
    await conn.execute(
        """
        UPDATE corpus_eval_runs SET engine = engine || $2::jsonb WHERE id = $1
        """,
        run_id,
        engine,
    )


async def insert_run_items(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    run_id: UUID,
    items: list[tuple[str, str, str, str | None]],
) -> None:
    """``items`` are (script_id, condition, status, error) — a run is created
    with every RECORDING already listed, so progress is a count against a
    known total from the first tick instead of a number that grows out of
    nowhere. A paired replica contributes two rows, which is the point."""
    await conn.executemany(
        """
        INSERT INTO corpus_eval_run_items
            (tenant_id, run_id, script_id, condition, status, error)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        [
            (tenant_id, run_id, sid, condition, status, error)
            for sid, condition, status, error in items
        ],
    )


async def get_run(conn: asyncpg.Connection, run_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow(
        """
        SELECT r.id, r.snapshot_id, r.status, r.model, r.started_by,
               r.started_at, r.finished_at, r.summary, r.dataset,
               r.normalizer_version, r.corpus_sha256, r.engine, r.bootstrap_seed,
               s.version AS snapshot_version
        FROM corpus_eval_runs r
        JOIN corpus_eval_snapshots s ON s.id = r.snapshot_id
        WHERE r.id = $1
        """,
        run_id,
    )


async def list_runs(
    conn: asyncpg.Connection, limit: int = 20, dataset: str | None = None
) -> list[asyncpg.Record]:
    """Newest first, optionally one set only. §7's run table is the surface
    that answers "which configuration is better", and mixing dev and test
    rows into one sortable list is precisely how a dev number gets quoted as
    the official one."""
    return list(
        await conn.fetch(
            """
            SELECT r.id, r.snapshot_id, r.status, r.model, r.started_by,
                   r.started_at, r.finished_at, r.summary, r.dataset,
                   r.normalizer_version, r.corpus_sha256, r.engine,
                   r.bootstrap_seed, s.version AS snapshot_version
            FROM corpus_eval_runs r
            JOIN corpus_eval_snapshots s ON s.id = r.snapshot_id
            WHERE ($2::text IS NULL OR r.dataset = $2)
            ORDER BY r.started_at DESC
            LIMIT $1
            """,
            limit,
            dataset,
        )
    )


async def list_run_items(
    conn: asyncpg.Connection, run_id: UUID
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_RUN_ITEM_COLS}
            FROM corpus_eval_run_items
            WHERE run_id = $1
            ORDER BY script_id, condition
            """,
            run_id,
        )
    )


async def in_flight_items(
    conn: asyncpg.Connection, *, run_id: UUID, limit: int
) -> list[asyncpg.Record]:
    """Items with an ASR job to poll."""
    return list(
        await conn.fetch(
            f"""
            SELECT {_RUN_ITEM_COLS}
            FROM corpus_eval_run_items
            WHERE run_id = $1 AND status = 'transcribing' AND asr_job_id IS NOT NULL
            ORDER BY script_id, condition
            LIMIT $2
            """,
            run_id,
            limit,
        )
    )


async def claim_pending(
    conn: asyncpg.Connection, *, run_id: UUID, limit: int
) -> list[asyncpg.Record]:
    """Take the next pending utterances and mark them claimed IN THE SAME
    STATEMENT.

    The pump talks HTTP to asr-service, which must not happen while a
    database transaction is held open — so the claim cannot be a row lock
    spanning the call. Instead a claimed item is `transcribing` with a NULL
    job id: visible to every other pump as "someone is submitting this",
    and recoverable by ``reset_stale_claims`` if that someone's tab closed
    mid-submit. SKIP LOCKED keeps two simultaneous pumps from claiming the
    same row inside the statement itself.
    """
    return list(
        await conn.fetch(
            f"""
            UPDATE corpus_eval_run_items
               SET status = 'transcribing', asr_job_id = NULL, updated_at = now()
             WHERE id IN (
                 SELECT id FROM corpus_eval_run_items
                  WHERE run_id = $1 AND status = 'pending'
                  ORDER BY script_id, condition
                  LIMIT $2
                  FOR UPDATE SKIP LOCKED
             )
            RETURNING {_RUN_ITEM_COLS}
            """,
            run_id,
            limit,
        )
    )


async def release_item(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    script_id: str,
    condition: str,
    error: str | None,
) -> None:
    """Hand a claimed item back to the queue — asr-service was busy or
    briefly unreachable. ``error`` is kept as the last reason so a run that
    stalls says why, but the item stays retryable."""
    await conn.execute(
        """
        UPDATE corpus_eval_run_items
           SET status = 'pending', asr_job_id = NULL, error = $4, updated_at = now()
         WHERE run_id = $1 AND script_id = $2 AND condition = $3
        """,
        run_id,
        script_id,
        condition,
        error,
    )


async def reset_stale_claims(
    conn: asyncpg.Connection, *, run_id: UUID, older_than_seconds: int
) -> int:
    """Recover claims whose submit never completed (closed tab, crash).

    Only ever touches rows with a NULL job id: an item that HAS a job id is
    genuinely transcribing, however long it takes, and resetting it would
    submit the same audio twice.
    """
    tag = await conn.execute(
        """
        UPDATE corpus_eval_run_items
           SET status = 'pending', updated_at = now()
         WHERE run_id = $1
           AND status = 'transcribing'
           AND asr_job_id IS NULL
           AND updated_at < now() - make_interval(secs => $2)
        """,
        run_id,
        older_than_seconds,
    )
    return int(tag.rsplit(" ", 1)[-1]) if tag.startswith("UPDATE") else 0


async def count_by_status(conn: asyncpg.Connection, run_id: UUID) -> dict[str, int]:
    rows = await conn.fetch(
        """
        SELECT status, count(*) AS n
        FROM corpus_eval_run_items
        WHERE run_id = $1
        GROUP BY status
        """,
        run_id,
    )
    return {str(r["status"]): int(r["n"]) for r in rows}


async def mark_item_transcribing(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    script_id: str,
    condition: str,
    asr_job_id: UUID,
) -> None:
    await conn.execute(
        """
        UPDATE corpus_eval_run_items
           SET status = 'transcribing', asr_job_id = $4, error = NULL,
               updated_at = now()
         WHERE run_id = $1 AND script_id = $2 AND condition = $3
        """,
        run_id,
        script_id,
        condition,
        asr_job_id,
    )


async def mark_item_scored(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    script_id: str,
    condition: str,
    hypothesis: str,
    wer: float,
    cer: float,
    ref_words: int,
    wer_norm: float,
    cer_norm: float,
    ref_words_norm: int,
    ref_chars_norm: int,
    dose_tokens: int,
    dose_exact: bool,
    flags: str,
    speech_ms: int | None,
) -> None:
    """Both scores, the dose verdict and the flags land in ONE statement.

    They are one observation of one utterance; writing them separately would
    admit a state where an item is scored raw but not normalised, which
    every aggregate downstream would then have to defend against.
    """
    await conn.execute(
        """
        UPDATE corpus_eval_run_items
           SET status = 'scored', hypothesis = $4, wer = $5, cer = $6,
               ref_words = $7, wer_norm = $8, cer_norm = $9,
               ref_words_norm = $10, ref_chars_norm = $11, dose_tokens = $12,
               dose_exact = $13, flags = $14::jsonb, speech_ms = $15,
               error = NULL, updated_at = now()
         WHERE run_id = $1 AND script_id = $2 AND condition = $3
        """,
        run_id,
        script_id,
        condition,
        hypothesis,
        wer,
        cer,
        ref_words,
        wer_norm,
        cer_norm,
        ref_words_norm,
        ref_chars_norm,
        dose_tokens,
        dose_exact,
        flags,
        speech_ms,
    )


async def mark_item_failed(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    script_id: str,
    condition: str,
    error: str,
) -> None:
    await conn.execute(
        """
        UPDATE corpus_eval_run_items
           SET status = 'failed', error = $4, updated_at = now()
         WHERE run_id = $1 AND script_id = $2 AND condition = $3
        """,
        run_id,
        script_id,
        condition,
        error,
    )


async def note_run_model(conn: asyncpg.Connection, *, run_id: UUID, model: str) -> None:
    """Record the engine the first completed job reported.

    A run is created before anything is known about what will transcribe it,
    so ``model`` starts as 'unknown'. It is filled in as soon as reality
    answers, and never overwritten afterwards — if a fleet somehow served
    two models to one run, the first one is at least a fact, whereas
    last-writer-wins would be a coin toss.
    """
    await conn.execute(
        """
        UPDATE corpus_eval_runs SET model = $2
         WHERE id = $1 AND model = 'unknown'
        """,
        run_id,
        model,
    )


async def scored_items(conn: asyncpg.Connection, run_id: UUID) -> list[asyncpg.Record]:
    """Scored items joined to the recordings they measured — the summary's
    input. Only ``status='scored'``: a failed or skipped utterance is absent
    from both halves of the average, never a zero.

    THE JOIN CARRIES THE CONDITION (0095). Without it a paired replica's two
    scores would each match both of its snapshot rows, and every paired line
    would contribute four rows to an average that should have two.
    """
    return list(
        await conn.fetch(
            """
            SELECT ri.script_id, ri.condition, ri.wer, ri.cer, ri.ref_words,
                   ri.hypothesis,
                   ri.wer_norm, ri.cer_norm, ri.ref_words_norm, ri.ref_chars_norm,
                   ri.dose_tokens, ri.dose_exact, ri.flags, ri.speech_ms,
                   si.subset, si.language, si.dataset, si.paired,
                   si.transcript AS reference,
                   char_length(si.transcript) AS ref_chars
            FROM corpus_eval_run_items ri
            JOIN corpus_eval_runs r ON r.id = ri.run_id
            JOIN corpus_eval_snapshot_items si
              ON si.snapshot_id = r.snapshot_id
             AND si.script_id = ri.script_id
             AND si.condition = ri.condition
            WHERE ri.run_id = $1 AND ri.status = 'scored'
            ORDER BY ri.script_id, ri.condition
            """,
            run_id,
        )
    )


async def finish_run(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    status: str,
    model: str,
    summary: str,
) -> asyncpg.Record | None:
    """Written once, when no item is left pending or transcribing. ``model``
    is refreshed from what asr-service actually reported: the row is created
    with the configured expectation and corrected by reality."""
    return await conn.fetchrow(
        f"""
        UPDATE corpus_eval_runs
           SET status = $2, model = $3, summary = $4::jsonb, finished_at = now()
         WHERE id = $1
        RETURNING {_RUN_COLS}
        """,
        run_id,
        status,
        model,
        summary,
    )


# ══ import journal (migration 0092, §6) ════════════════════════════════

_IMPORT_COLS = """
    id, filename, file_sha256, dry_run, rows_total, rows_added, rows_skipped,
    rows_rejected, report, imported_by, created_at
"""


async def insert_import(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    filename: str,
    file_sha256: str,
    dry_run: bool,
    rows_total: int,
    rows_added: int,
    rows_skipped: int,
    rows_rejected: int,
    report: str,
    imported_by: UUID,
) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"""
        INSERT INTO corpus_eval_imports
            (tenant_id, filename, file_sha256, dry_run, rows_total, rows_added,
             rows_skipped, rows_rejected, report, imported_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
        RETURNING {_IMPORT_COLS}
        """,
        tenant_id,
        filename,
        file_sha256,
        dry_run,
        rows_total,
        rows_added,
        rows_skipped,
        rows_rejected,
        report,
        imported_by,
    )
    assert row is not None
    return row


async def list_imports(
    conn: asyncpg.Connection, limit: int = 20
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_IMPORT_COLS} FROM corpus_eval_imports
            ORDER BY created_at DESC LIMIT $1
            """,
            limit,
        )
    )


# ══ take-attempt journal (migration 0092, §7) ══════════════════════════


async def insert_take_attempt(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    script_id: str,
    take_id: UUID | None,
    speaker: UUID,
    device: str | None,
    condition: str,
    duration_ms: int,
    audio_sha256: str | None,
    status: str,
    reason: str | None,
    expected_condition: str | None = None,
) -> None:
    """One row per attempt. Never updated — see the 0092 header.

    ``expected_condition`` is what the REPLICA asked for (0094). Storing it
    beside the condition actually used is what makes ``condition_mismatch``
    derivable here rather than at every call site — and readable later
    without joining to a script row that may since have been edited.
    """
    await conn.execute(
        """
        INSERT INTO corpus_eval_take_attempts
            (tenant_id, script_id, take_id, speaker, device, condition,
             duration_ms, audio_sha256, status, reason,
             expected_condition, condition_mismatch)
        -- $11 is cast because it appears only in comparisons: without a
        -- type Postgres refuses to prepare the statement ("could not
        -- determine data type of parameter $11"), which is a runtime
        -- failure on every take upload, not a warning.
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::text,
                $11::text IS NOT NULL AND $11::text IS DISTINCT FROM $6)
        """,
        tenant_id,
        script_id,
        take_id,
        speaker,
        device,
        condition,
        duration_ms,
        audio_sha256,
        status,
        reason,
        expected_condition,
    )


async def list_take_attempts(
    conn: asyncpg.Connection, *, script_id: str | None = None, limit: int = 500
) -> list[asyncpg.Record]:
    """The journal, with the attempt number and supersession DERIVED.

    ``attempt_n`` is a window function rather than a stored counter: two
    colleagues recording the same line in the same second would otherwise
    race for the same number, and a journal that can 500 on a write is a
    journal with holes in it. ``superseded`` marks every saved attempt that
    a later saved attempt replaced — the corpus keeps one take per line, so
    all but the last are history.

    SUPERSESSION IS PER (LINE, CONDITION) since 0095. A paired replica holds
    one take per condition, so the headset recording does not replace the
    phone one — partitioning by script_id alone would mark whichever was
    recorded first as superseded and quietly drop half the paired design.
    ``attempt_n`` stays per LINE, because "скільки спроб коштує репліка"
    (§7) is a question about the line's total cost.
    """
    return list(
        await conn.fetch(
            """
            SELECT id, script_id, take_id, speaker, device, condition,
                   expected_condition, condition_mismatch,
                   duration_ms, audio_sha256, status, reason, created_at,
                   row_number() OVER (
                       PARTITION BY script_id ORDER BY created_at, id
                   ) AS attempt_n,
                   (status = 'saved' AND EXISTS (
                       SELECT 1 FROM corpus_eval_take_attempts later
                        WHERE later.script_id = a.script_id
                          AND later.condition = a.condition
                          AND later.status = 'saved'
                          AND (later.created_at, later.id) > (a.created_at, a.id)
                   )) AS superseded
            FROM corpus_eval_take_attempts a
            WHERE ($1::text IS NULL OR script_id = $1)
            ORDER BY script_id, created_at, id
            LIMIT $2
            """,
            script_id,
            limit,
        )
    )


async def attempt_counts(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Attempts per line — "скільки спроб коштує репліка" (§7)."""
    return list(
        await conn.fetch(
            """
            SELECT script_id,
                   count(*) AS attempts,
                   count(*) FILTER (WHERE status = 'saved')     AS saved,
                   count(*) FILTER (WHERE status = 'discarded') AS discarded,
                   count(*) FILTER (WHERE status = 'rejected')  AS rejected,
                   count(*) FILTER (WHERE condition_mismatch) AS mismatches,
                   count(DISTINCT speaker) AS speakers,
                   count(DISTINCT condition) FILTER (WHERE status = 'saved')
                       AS conditions_recorded,
                   max(created_at) AS last_attempt_at
            FROM corpus_eval_take_attempts
            GROUP BY script_id
            ORDER BY script_id
            """
        )
    )


# ══ gold-transcript revisions (migration 0093, corpus-v3 Epic B) ════════

_GOLD_REV_COLS = """
    id, script_id, dataset, old_transcript, new_transcript, reason,
    normalizer_version, canonical_equal, revised_by, created_at
"""


async def insert_gold_revision(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    script_id: str,
    dataset: str,
    old_transcript: str,
    new_transcript: str,
    reason: str,
    normalizer_version: str,
    canonical_equal: bool,
    revised_by: UUID | None,
) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"""
        INSERT INTO corpus_eval_gold_revisions
            (tenant_id, script_id, dataset, old_transcript, new_transcript,
             reason, normalizer_version, canonical_equal, revised_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING {_GOLD_REV_COLS}
        """,
        tenant_id,
        script_id,
        dataset,
        old_transcript,
        new_transcript,
        reason,
        normalizer_version,
        canonical_equal,
        revised_by,
    )
    assert row is not None
    return row


async def list_gold_revisions(
    conn: asyncpg.Connection, *, script_id: str | None = None, limit: int = 200
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT {_GOLD_REV_COLS} FROM corpus_eval_gold_revisions
             WHERE ($1::text IS NULL OR script_id = $1)
             ORDER BY created_at DESC
             LIMIT $2
            """,
            script_id,
            limit,
        )
    )


async def script_ids_revised_since(
    conn: asyncpg.Connection, *, since: datetime
) -> set[str]:
    """Lines whose gold changed after ``since`` — the "еталон змінено" mark.

    Derived per read rather than stamped onto run rows: a run's stored score
    is a fact about a comparison that happened, and rewriting it later to
    say "…but the reference moved" would be editing history to record that
    history was edited. The mark belongs to the READING, so it is computed
    when the run is read.
    """
    rows = await conn.fetch(
        """
        SELECT DISTINCT script_id FROM corpus_eval_gold_revisions
         WHERE created_at > $1
        """,
        since,
    )
    return {r["script_id"] for r in rows}


# ══ recording instructions (migration 0094, corpus-v3 Epic C) ══════════


async def instruction_templates(
    conn: asyncpg.Connection, *, lang_ui: str
) -> list[asyncpg.Record]:
    """Both halves of every instruction, tenant rows shadowing system ones.

    The shadowing is done here rather than in two queries because "which
    template wins" is one question with one answer, and splitting it across
    a call site is how a tenant override ends up applying to the condition
    sentence but not the category one.
    """
    return list(
        await conn.fetch(
            """
            SELECT DISTINCT ON (condition, category)
                   scope, condition, category, lang_ui, text
              FROM corpus_eval_instruction_templates
             WHERE lang_ui = $1
             ORDER BY condition, category, scope DESC
            """,
            lang_ui,
        )
    )


# ══ the retake queue (migration 0096, corpus-v3 Epic E) ════════════════


async def retake_candidates(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Every take that needs re-recording, with the reasons, DERIVED.

    Four signals, three of them computed from evidence rather than stored:

      · the human's "брак" mark, the only stored one (0096);
      · condition_mismatch, from the attempt that produced this take;
      · the scoring flags (silence, hallucination, WER > 100%) from the most
        recent run that scored it.

    EVERY DERIVED SIGNAL IS DATED AGAINST THE TAKE'S AUDIO. A flag from a run
    that scored the PREVIOUS recording is not a reason to re-record the
    current one, and that comparison is the whole mechanism behind Epic E's
    acceptance criterion: re-record and the line leaves the queue, with
    nothing to remember to clear.
    """
    return list(
        await conn.fetch(
            """
            SELECT t.script_id,
                   t.condition,
                   t.updated_at,
                   t.duration_ms,
                   t.flagged_bad,
                   t.flagged_note,
                   COALESCE(a.condition_mismatch, false) AS condition_mismatch,
                   COALESCE(f.flags, '[]'::jsonb)        AS run_flags,
                   f.run_started_at
              FROM corpus_eval_takes t
              -- The attempt that produced this take, matched on the audio
              -- digest: an attempt row survives its take being replaced, so
              -- matching on script_id alone would resurrect an old mismatch.
              LEFT JOIN LATERAL (
                  SELECT at.condition_mismatch
                    FROM corpus_eval_take_attempts at
                   WHERE at.script_id = t.script_id
                     AND at.audio_sha256 = t.audio_sha256
                     AND at.status = 'saved'
                   ORDER BY at.created_at DESC
                   LIMIT 1
              ) a ON true
              -- The newest run that scored this recording AFTER it was made.
              LEFT JOIN LATERAL (
                  SELECT ri.flags, r.started_at AS run_started_at
                    FROM corpus_eval_run_items ri
                    JOIN corpus_eval_runs r ON r.id = ri.run_id
                   WHERE ri.script_id = t.script_id
                     AND ri.condition = t.condition
                     AND ri.status = 'scored'
                     AND ri.flags <> '[]'::jsonb
                     AND r.started_at > t.updated_at
                   ORDER BY r.started_at DESC
                   LIMIT 1
              ) f ON true
             WHERE t.flagged_bad
                OR COALESCE(a.condition_mismatch, false)
                OR f.flags IS NOT NULL
             ORDER BY t.script_id, t.condition
            """
        )
    )


# ══ speaker consents and the data register (migration 0097, Epic F) ════

_CONSENT_COLS = """
    id, speaker_id, scope, granted_at, granted_by, revoked_at, revoked_by, note
"""


async def list_consents(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Consent history, newest first. Revoked rows stay: "this person
    consented in March and withdrew in August" is the record an auditor
    asks for, and a table that only shows the current state cannot give it."""
    return list(
        await conn.fetch(
            f"""
            SELECT {_CONSENT_COLS},
                   (SELECT count(*) FROM corpus_eval_takes t
                     WHERE t.recorded_by = c.speaker_id) AS takes_recorded
              FROM corpus_speaker_consents c
             ORDER BY granted_at DESC
            """
        )
    )


async def grant_consent(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    speaker_id: UUID,
    granted_by: UUID,
    note: str | None,
) -> asyncpg.Record | None:
    """None when an active consent already exists — the partial unique index
    makes re-granting a no-op rather than a second live row."""
    return await conn.fetchrow(
        f"""
        INSERT INTO corpus_speaker_consents
            (tenant_id, speaker_id, scope, granted_by, note)
        VALUES ($1, $2, 'corpus_voice', $3, $4)
        ON CONFLICT DO NOTHING
        RETURNING {_CONSENT_COLS}
        """,
        tenant_id,
        speaker_id,
        granted_by,
        note,
    )


async def revoke_consent(
    conn: asyncpg.Connection, *, speaker_id: UUID, revoked_by: UUID
) -> asyncpg.Record | None:
    """Withdrawal. Deletes nothing — see the 0097 header on why erasure of
    the audio is a separate, deliberate act."""
    return await conn.fetchrow(
        f"""
        UPDATE corpus_speaker_consents
           SET revoked_at = now(), revoked_by = $2
         WHERE speaker_id = $1 AND scope = 'corpus_voice' AND revoked_at IS NULL
        RETURNING {_CONSENT_COLS}
        """,
        speaker_id,
        revoked_by,
    )


_REGISTRY_COLS = """
    id, name, version, sha256, purpose, data_origin, contains_patient_data,
    contains_personal_data, speakers, legal_basis, retention_period,
    storage_location, frozen, source_kind, source_id, utterances,
    created_at, updated_at
"""


async def list_registry(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"SELECT {_REGISTRY_COLS} FROM dataset_registry ORDER BY created_at DESC"
        )
    )


async def upsert_registry_entry(
    conn: asyncpg.Connection,
    *,
    tenant_id: UUID,
    name: str,
    version: str,
    sha256: str,
    purpose: str,
    data_origin: str,
    contains_personal_data: bool,
    speakers: list[UUID],
    legal_basis: str,
    retention_period: str,
    storage_location: str,
    frozen: bool,
    source_kind: str,
    source_id: UUID | None,
    utterances: int | None,
) -> asyncpg.Record:
    """Idempotent by (tenant, name, version): re-registering the same
    artefact refreshes it instead of accumulating near-duplicate rows an
    auditor then has to tell apart."""
    row = await conn.fetchrow(
        f"""
        INSERT INTO dataset_registry
            (tenant_id, name, version, sha256, purpose, data_origin,
             contains_personal_data, speakers, legal_basis, retention_period,
             storage_location, frozen, source_kind, source_id, utterances)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        ON CONFLICT ON CONSTRAINT dataset_registry_identity DO UPDATE SET
            sha256 = EXCLUDED.sha256,
            purpose = EXCLUDED.purpose,
            data_origin = EXCLUDED.data_origin,
            contains_personal_data = EXCLUDED.contains_personal_data,
            speakers = EXCLUDED.speakers,
            legal_basis = EXCLUDED.legal_basis,
            retention_period = EXCLUDED.retention_period,
            storage_location = EXCLUDED.storage_location,
            frozen = EXCLUDED.frozen,
            source_kind = EXCLUDED.source_kind,
            source_id = EXCLUDED.source_id,
            utterances = EXCLUDED.utterances,
            updated_at = now()
        RETURNING {_REGISTRY_COLS}
        """,
        tenant_id,
        name,
        version,
        sha256,
        purpose,
        data_origin,
        contains_personal_data,
        speakers,
        legal_basis,
        retention_period,
        storage_location,
        frozen,
        source_kind,
        source_id,
        utterances,
    )
    assert row is not None
    return row
