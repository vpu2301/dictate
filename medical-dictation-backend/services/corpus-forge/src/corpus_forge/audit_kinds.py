"""Audit event kinds emitted by corpus-forge (sprint 21).

Documented in docs/audit/event-kinds.md; the drift test there covers this
module. All corpus events are fleet-level and are written under the reserved
global tenant (nil UUID, migration 0068).
"""

CANDIDATE_GENERATED = "corpus.candidate_generated"
CANDIDATE_REVIEWED = "corpus.candidate_reviewed"
AUTO_ACCEPTED = "corpus.auto_accepted"
RELEASE_PUBLISHED = "corpus.release_published"
MINING_RUN = "corpus.mining_run"
JURY_DISAGREEMENT = "corpus.jury_disagreement"
