"""6-stage NLP pipeline implementations.

Order is the contract: voice_commands → punctuation → number_norm →
date_norm → abbreviation → confidence. Sprint 7 evals + sprint 8
reports + sprint 13 anamnesis all assume this order.
"""

from .abbreviation import AbbreviationStage
from .confidence import ConfidenceStage
from .date_norm import DateNormStage
from .number_norm import NumberNormStage
from .operations import operations_for
from .punctuation import PunctuationStage
from .voice_command_matcher import VoiceCommandMatcher
from .voice_commands import CommandSpec, VoiceCommandStage

__all__ = [
    "AbbreviationStage",
    "CommandSpec",
    "ConfidenceStage",
    "DateNormStage",
    "NumberNormStage",
    "PunctuationStage",
    "VoiceCommandMatcher",
    "VoiceCommandStage",
    "operations_for",
]
