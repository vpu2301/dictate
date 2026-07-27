"""Speaker diarization for conversation mode (sprint 14, ADR-0034).

Pipeline: Silero VAD segmentation → ECAPA-TDNN speaker embeddings →
online 2-speaker cosine clustering → word-level majority-overlap
attribution against Whisper word timings. Labels are anonymous S1/S2
proposals with confidence; UNKNOWN is emitted whenever the evidence is
ambiguous — never a guess. The doctor/patient mapping is a separate,
explainable inference (`mapping.py`) that freezes on manual override.
"""

from .attribution import AttributionPolicy, attribute_word
from .clustering import ClusteringConfig, OnlineSpeakerClusterer
from .embedder import EcapaEmbedder
from .mapping import MappingHypothesis, SpeakerMappingInference
from .stream import DiarizationConfig, DiarizationStream, SpeakerSegment
from .vad import SileroSegmenter

__all__ = [
    "AttributionPolicy",
    "ClusteringConfig",
    "DiarizationConfig",
    "DiarizationStream",
    "EcapaEmbedder",
    "MappingHypothesis",
    "OnlineSpeakerClusterer",
    "SileroSegmenter",
    "SpeakerMappingInference",
    "SpeakerSegment",
    "attribute_word",
]
