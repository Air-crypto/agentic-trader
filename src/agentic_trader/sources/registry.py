from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..picker.models import EvidenceVersion


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").casefold().rstrip(".")


def _matches(host: str, domain: str) -> bool:
    canonical = domain.casefold().strip().lstrip(".").rstrip(".")
    return host == canonical or host.endswith(f".{canonical}")


def quote_is_grounded(document: str, quote: str) -> bool:
    def normalize(value: str) -> str:
        return re.sub(r"\s+", " ", value).strip().casefold()

    normalized_quote = normalize(quote)
    return len(normalized_quote) >= 20 and normalized_quote in normalize(document)


@dataclass(frozen=True)
class SourceCheck:
    accepted: bool
    issuer_verified: bool
    reason: str = ""


@dataclass(frozen=True)
class SourceRegistry:
    schema_version: int
    reviewed_at: str
    issuers: dict[str, tuple[str, ...]]
    exchange_domains: tuple[str, ...]
    social_domains: tuple[str, ...]

    @classmethod
    def from_path(cls, path: str | Path) -> SourceRegistry:
        raw = json.loads(Path(path).read_text())
        if int(raw.get("schema_version", 0)) != 1:
            raise ValueError("Unsupported issuer-domain registry schema")
        issuers = {
            str(symbol).upper(): tuple(str(domain).casefold() for domain in domains)
            for symbol, domains in raw.get("issuers", {}).items()
        }
        return cls(
            schema_version=1,
            reviewed_at=str(raw["reviewed_at"]),
            issuers=issuers,
            exchange_domains=tuple(
                str(domain).casefold() for domain in raw.get("exchange_domains", [])
            ),
            social_domains=tuple(
                str(domain).casefold() for domain in raw.get("social_domains", [])
            ),
        )

    @classmethod
    def default(cls) -> SourceRegistry:
        root = Path(__file__).resolve().parents[3]
        return cls.from_path(root / "config" / "issuer_domains.json")

    def _host_in(self, host: str, domains: tuple[str, ...]) -> bool:
        return any(_matches(host, domain) for domain in domains)

    def check(self, evidence: EvidenceVersion) -> SourceCheck:
        host = _host(evidence.url)
        if evidence.source_type == "sec_filing" or _matches(host, "sec.gov"):
            return SourceCheck(False, False, "sec_source_disabled")
        if evidence.source_type == "issuer_primary":
            domains = self.issuers.get(evidence.symbol, ())
            if not domains:
                return SourceCheck(False, False, "symbol_missing_from_issuer_registry")
            if not self._host_in(host, domains):
                return SourceCheck(False, False, "issuer_domain_mismatch")
            return SourceCheck(True, True)
        if evidence.source_type == "exchange_notice":
            if not evidence.symbol:
                return SourceCheck(False, False, "exchange_notice_missing_symbol")
            if not self._host_in(host, self.exchange_domains):
                return SourceCheck(False, False, "exchange_domain_mismatch")
            return SourceCheck(True, True)
        if evidence.source_type == "government_record":
            if not host.endswith(".gov"):
                return SourceCheck(False, False, "government_domain_mismatch")
            return SourceCheck(True, False)
        if evidence.source_type == "social_unverified":
            if not self._host_in(host, self.social_domains):
                return SourceCheck(False, False, "unsupported_social_domain")
            return SourceCheck(True, False)
        if evidence.source_type == "reputable_reporting":
            return SourceCheck(True, False)
        return SourceCheck(False, False, "unsupported_source_type")
