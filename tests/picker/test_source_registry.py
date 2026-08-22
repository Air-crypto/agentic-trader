from __future__ import annotations

from dataclasses import replace

from agentic_trader.picker.models import EvidenceVersion
from agentic_trader.sources.registry import SourceRegistry, quote_is_grounded


def test_registry_verifies_exact_or_subdomain_but_not_suffix_spoof(evidence):
    registry = SourceRegistry(
        schema_version=1,
        reviewed_at="2026-08-21",
        issuers={"EXM": ("example.com",)},
        exchange_domains=("nyse.com",),
        social_domains=("reddit.com", "x.com"),
    )
    issuer = replace(
        evidence[0],
        source_type="issuer_primary",
        authority="issuer",
        url="https://investors.example.com/news",
    )
    assert registry.check(issuer).issuer_verified
    assert not registry.check(replace(issuer, url="https://example.com.evil.test")).accepted


def test_social_is_discovery_only_and_sec_is_disabled(evidence):
    registry = SourceRegistry(
        schema_version=1,
        reviewed_at="2026-08-21",
        issuers={},
        exchange_domains=(),
        social_domains=("reddit.com", "x.com"),
    )
    social = EvidenceVersion.from_dict(
        {
            **evidence[0].to_dict(),
            "source_type": "social_unverified",
            "authority": "social",
            "url": "https://www.reddit.com/r/stocks/comments/example",
            "primary": False,
            "issuer_verified": False,
        }
    )
    assert registry.check(social).accepted
    assert not registry.check(social).issuer_verified
    legacy_sec = EvidenceVersion.from_dict(
        {
            **evidence[0].to_dict(),
            "source_type": "sec_filing",
            "authority": "sec",
            "url": "https://www.sec.gov/Archives/example",
        }
    )
    assert registry.check(legacy_sec).reason == "sec_source_disabled"


def test_generic_quote_grounding_has_no_sec_dependency():
    assert quote_is_grounded(
        "The issuer raised its annual revenue guidance by ten percent.",
        "issuer raised its annual revenue guidance",
    )
