"""Structure-aware chunking (spec §1).

Sections are hard boundaries: a chunk never crosses a heading. Inside a
section, sentences accumulate toward the target token count; a section that
is one table (tab-separated rows) stays whole even when oversized. Token
counts are whitespace-word approximations — good enough for sizing, cheap
enough for 100k-chunk runs.

Character offsets are relative to the canonical full text (sections joined
by "\n\n" in reading order), matching the S01 Chunk contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from evidence_ingest.domain.parse_types import ParsedDocument

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-ZА-ЯІЇЄҐ0-9«\"(])")


@dataclass(frozen=True, slots=True)
class ChunkSpan:
    section_path: str
    char_start: int
    char_end: int
    text: str


def _tokens(text: str) -> int:
    return len(text.split())


def _is_table(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return False
    tab_lines = sum(1 for ln in lines if "\t" in ln)
    return tab_lines >= len(lines) // 2 + 1


def _split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def chunk_document(
    doc: ParsedDocument,
    *,
    target_tokens: int = 500,
    max_tokens: int = 700,
    min_tokens: int = 350,
) -> list[ChunkSpan]:
    chunks: list[ChunkSpan] = []
    offset = 0
    for section in doc.sections:
        text = section.text
        if not text.strip():
            continue
        base = offset
        if _is_table(text) or _tokens(text) <= max_tokens:
            chunks.append(
                ChunkSpan(
                    section_path=section.path,
                    char_start=base,
                    char_end=base + len(text),
                    text=text,
                )
            )
        else:
            # Accumulate sentences toward the target; flush before max.
            pieces = _split_sentences(text) or [text]
            buffer: list[str] = []
            buffer_tokens = 0
            piece_start = 0
            cursor = 0
            prev_end = 0
            for piece in pieces:
                piece_pos = text.index(piece, cursor)
                cursor = piece_pos + len(piece)
                piece_token_count = _tokens(piece)
                if buffer and buffer_tokens + piece_token_count > max_tokens:
                    chunk_text = text[piece_start:prev_end]
                    chunks.append(
                        ChunkSpan(
                            section_path=section.path,
                            char_start=base + piece_start,
                            char_end=base + prev_end,
                            text=chunk_text,
                        )
                    )
                    piece_start = piece_pos
                    buffer = []
                    buffer_tokens = 0
                buffer.append(piece)
                buffer_tokens += piece_token_count
                prev_end = cursor
                if buffer_tokens >= target_tokens:
                    chunk_text = text[piece_start:prev_end]
                    chunks.append(
                        ChunkSpan(
                            section_path=section.path,
                            char_start=base + piece_start,
                            char_end=base + prev_end,
                            text=chunk_text,
                        )
                    )
                    piece_start = cursor
                    buffer = []
                    buffer_tokens = 0
            if buffer:
                tail = text[piece_start:]
                if (
                    chunks
                    and _tokens(tail) < min_tokens
                    and chunks[-1].char_end == base + piece_start
                    and chunks[-1].section_path == section.path
                ):
                    last = chunks.pop()
                    merged = text[last.char_start - base : len(text)]
                    chunks.append(
                        ChunkSpan(
                            section_path=section.path,
                            char_start=last.char_start,
                            char_end=base + len(text),
                            text=merged,
                        )
                    )
                else:
                    chunks.append(
                        ChunkSpan(
                            section_path=section.path,
                            char_start=base + piece_start,
                            char_end=base + len(text),
                            text=tail,
                        )
                    )
        offset = base + len(text) + 2  # the "\n\n" joiner
    return chunks
