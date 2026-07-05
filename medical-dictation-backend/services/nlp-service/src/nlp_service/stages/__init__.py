"""NLP pipeline stage implementations.

Order is the contract: voice_commands → punctuation → spoken_punctuation →
number_norm → date_norm → abbreviation → confidence. Sprint 7 evals +
sprint 8 reports + sprint 13 anamnesis all assume this order.
"""

from .abbreviation import AbbreviationStage
from .confidence import ConfidenceStage
from .date_norm import DateNormStage
from .number_norm import NumberNormStage
from .operations import operations_for
from .punctuation import PunctuationStage
from .spoken_punctuation import normalize_spoken_punctuation
from .spoken_punctuation_stage import SpokenPunctuationStage
from .voice_command_matcher import VoiceCommandMatcher
from .voice_commands import CommandSpec, VoiceCommandStage

__all__ = [
    "AbbreviationStage",
    "CommandSpec",
    "ConfidenceStage",
    "DateNormStage",
    "NumberNormStage",
    "PunctuationStage",
    "SpokenPunctuationStage",
    "VoiceCommandMatcher",
    "VoiceCommandStage",
    "normalize_spoken_punctuation",
    "operations_for",
]
