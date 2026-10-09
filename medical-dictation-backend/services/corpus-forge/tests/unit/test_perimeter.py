"""The refuse-to-start assertion: public jury URL must raise."""

import pytest
from corpus_forge.domain.jury import PHIBoundaryViolation
from corpus_forge.domain.perimeter import assert_in_perimeter_url, is_private_host


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8089",
        "http://127.0.0.1:8089",
        "http://10.12.0.4:8089",
        "http://172.16.3.2:8089",
        "http://192.168.1.20:8089",
        "http://llama-server:8089",  # docker-compose single-label
        "http://gen-backend.internal:8089",
        "http://inference.gpu.svc:8089",
        "http://inference.ml.svc.cluster.local:8089",
    ],
)
def test_private_urls_pass(url: str) -> None:
    assert_in_perimeter_url(url, purpose="jury backend")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "https://api.anthropic.com",
        "http://34.120.8.5:8089",  # public IP literal
        "http://example.com:8089",
        "ftp://localhost:8089",  # wrong scheme
        "http://",  # no host
    ],
)
def test_public_or_malformed_urls_raise(url: str) -> None:
    with pytest.raises(PHIBoundaryViolation):
        assert_in_perimeter_url(url, purpose="jury backend")


def test_no_dns_resolution_is_involved() -> None:
    # A dotted name that HAPPENS to resolve privately in some environment is
    # still refused: the judgement is static, on the URL shape alone.
    assert not is_private_host("jury.mycorp.com")
