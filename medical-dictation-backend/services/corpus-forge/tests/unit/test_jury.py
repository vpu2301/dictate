"""Jury mechanics + THE test: a mined candidate routed to an external
endpoint must raise, not warn (plan §12)."""

import pytest
from corpus_forge.domain.jury import (
    JuryVote,
    PHIBoundaryViolation,
    enforce_phi_boundary,
    parse_vote,
    run_jury,
    tally,
)


class FakeClient:
    def __init__(self, *, in_perimeter: bool, responses: list[str] | None = None) -> None:
        self.in_perimeter = in_perimeter
        self.model_name = "fake-model"
        self._responses = responses or []
        self.calls = 0

    async def complete(self, prompt: str) -> str:
        self.calls += 1
        return self._responses[self.calls - 1]


EXTERNAL = FakeClient(in_perimeter=False)
LOCAL = FakeClient(in_perimeter=True)


class TestPHIBoundary:
    @pytest.mark.parametrize("kind", ["mined", "telemetry_gap"])
    def test_phi_derived_to_external_raises(self, kind: str) -> None:
        with pytest.raises(PHIBoundaryViolation):
            enforce_phi_boundary(kind, EXTERNAL)

    @pytest.mark.parametrize("kind", ["terminology", "generated", "authored"])
    def test_public_data_to_external_allowed(self, kind: str) -> None:
        enforce_phi_boundary(kind, EXTERNAL)

    @pytest.mark.parametrize("kind", ["mined", "telemetry_gap", "terminology", "generated"])
    def test_everything_in_perimeter_allowed(self, kind: str) -> None:
        enforce_phi_boundary(kind, LOCAL)

    async def test_run_jury_raises_before_any_network_call(self) -> None:
        client = FakeClient(in_perimeter=False, responses=['{"verdict":"accept","reason":"x"}'] * 3)
        with pytest.raises(PHIBoundaryViolation):
            await run_jury(
                client=client, source_kind="mined", prompts=["a", "b", "c"], prompt_version="v1"
            )
        assert client.calls == 0, "the boundary must trip before the first request"


class TestVoteParsing:
    def test_valid_json_parses(self) -> None:
        vote = parse_vote('{"verdict": "accept", "reason": "fine", "suggested_edit": null}')
        assert vote.verdict == "accept"

    def test_json_wrapped_in_prose_parses(self) -> None:
        vote = parse_vote('Sure! Here is my verdict:\n{"verdict": "reject", "reason": "generic"}')
        assert vote.verdict == "reject"

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            '{"verdict": "maybe", "reason": "?"}',  # invalid enum
            '{"verdict": "accept"}',  # missing reason
            '{"verdict": "accept", "reason": "ok", "extra": 1}',  # extra=forbid
            "",
        ],
    )
    def test_malformed_output_fails_closed_to_reject(self, raw: str) -> None:
        assert parse_vote(raw).verdict == "reject"


class TestTally:
    def _v(self, verdict: str) -> JuryVote:
        return JuryVote(verdict=verdict, reason="r", suggested_edit=None)  # type: ignore[arg-type]

    def test_unanimous(self) -> None:
        r = tally([self._v("accept")] * 3, model_name="m", prompt_version="v1")
        assert r.unanimous_accept and r.majority_accept and not r.disagreement
        assert r.engine == "jury:m:v1"

    def test_majority_split_is_disagreement(self) -> None:
        r = tally(
            [self._v("accept"), self._v("accept"), self._v("reject")],
            model_name="m",
            prompt_version="v1",
        )
        assert not r.unanimous_accept and r.majority_accept and r.disagreement

    def test_all_reject(self) -> None:
        r = tally([self._v("reject")] * 3, model_name="m", prompt_version="v1")
        assert not r.majority_accept and not r.disagreement
