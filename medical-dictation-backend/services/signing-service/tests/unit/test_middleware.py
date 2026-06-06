"""Middleware classifier — every route lands in the right chain."""

from __future__ import annotations

from signing_service.middleware import chain_for_path


def test_public_verify_routes():
    assert chain_for_path("/verify/abc") == "public_verify_chain"
    assert chain_for_path("/verify/abc/pdf") == "public_verify_chain"


def test_callback_routes():
    assert chain_for_path("/signing/callbacks/diia") == "callback_chain"
    assert chain_for_path("/signing/callbacks/iit") == "callback_chain"


def test_internal_routes():
    assert chain_for_path("/signing/sessions") == "internal_chain"
    assert chain_for_path("/signing/health") == "internal_chain"
    assert chain_for_path("/anything-else") == "internal_chain"
