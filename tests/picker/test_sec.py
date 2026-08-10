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


def test_recent_filings_parses_legacy_and_iso_acceptance_datetime(monkeypatch, tmp_path):
    client = SECClient(user_agent="agentic-trader test@example.com", cache_dir=tmp_path)
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000320193-26-000001", "0000320193-26-000002"],
                "form": ["8-K", "8-K"],
                "filingDate": ["2026-08-01", "2026-08-05"],
                "acceptanceDateTime": ["20260801093000", "2026-08-05T20:38:25.000Z"],
                "primaryDocument": ["ex1.htm", "ex2.htm"],
            }
        }
    }
    monkeypatch.setattr(client, "submissions", lambda cik: submissions)
    filings = client.recent_filings("0000320193")
    assert filings[0].accepted_at is not None
    assert filings[0].accepted_at.isoformat() == "2026-08-01T09:30:00+00:00"
    assert filings[1].accepted_at is not None
    assert filings[1].accepted_at.isoformat() == "2026-08-05T20:38:25+00:00"
