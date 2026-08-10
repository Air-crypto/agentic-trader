from __future__ import annotations

import pytest

from agentic_trader.sources.sec import SECClient


def test_sec_client_requires_identifying_user_agent(tmp_path):
    with pytest.raises(ValueError, match="contact email"):
        SECClient(user_agent="anonymous", cache_dir=tmp_path)


def test_grounded_quote_ignores_whitespace_and_case():
    document = "The Company\nRAISED annual guidance by ten percent."
    quote = "the company raised annual guidance by ten percent."
    assert SECClient.quote_is_grounded(document, quote)


def test_short_or_absent_quote_is_not_grounded():
    assert not SECClient.quote_is_grounded("Unrelated filing text", "short quote")
