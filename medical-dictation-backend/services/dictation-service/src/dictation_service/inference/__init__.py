"""Streaming inference primitives.

Pure-logic submodules (aligner, committer, prompt) export eagerly so
their tests don't have to install numpy. Windower + concurrency + vad
touch numpy and are lazy.
"""

from .aligner import AlignResult, align_overlap, normalized_levenshtein
from .committer import CommitDecision, Committer, words_to_final_segments
from .prompt import build_prompt, truncate_to_tokens

__all__ = [
    "AlignResult",
    "CommitDecision",
    "Committer",
    "InferenceQueue",
    "StreamingWindower",
    "WindowSlice",
    "WorkerCapacityError",
    "align_overlap",
    "build_prompt",
    "normalized_levenshtein",
    "truncate_to_tokens",
    "words_to_final_segments",
]


def __getattr__(name: str):
    if name in {"InferenceQueue", "WorkerCapacityError"}:
        from .concurrency import InferenceQueue, WorkerCapacityError

        return {"InferenceQueue": InferenceQueue, "WorkerCapacityError": WorkerCapacityError}[name]
    if name in {"StreamingWindower", "WindowSlice"}:
        from .windower import StreamingWindower, WindowSlice

        return {"StreamingWindower": StreamingWindower, "WindowSlice": WindowSlice}[name]
    raise AttributeError(name)
