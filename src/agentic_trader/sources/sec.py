from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

SEC_DATA_BASE = "https://data.sec.gov"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


@dataclass(frozen=True)
class SECFiling:
    cik: str
    accession_number: str
    form: str
    filed_at: date
    accepted_at: datetime | None
    primary_document: str
    url: str


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _parse_acceptance_datetime(raw: str) -> datetime:
    """Parse SEC's acceptanceDateTime, which has appeared in two formats:
    the legacy compact "%Y%m%d%H%M%S" and current ISO-8601 with a "Z" suffix.
    """
    try:
        return datetime.strptime(raw, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


class SECClient:
    """Small SEC EDGAR client that preserves point-in-time accession metadata."""

    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: str | Path = ".cache/sec",
        requests_per_second: float = 8.0,
    ) -> None:
        self.user_agent = user_agent or os.environ.get("SEC_USER_AGENT", "")
        if "@" not in self.user_agent:
            raise ValueError(
                "SEC_USER_AGENT must identify the application and contain a contact email"
            )
        if requests_per_second <= 0 or requests_per_second > 10:
            raise ValueError("SEC request rate must be within (0, 10]")
        self.cache_dir = Path(cache_dir)
        self.interval = 1.0 / requests_per_second
        self._last_request = 0.0

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.cache"

    def _request(self, url: str, max_age_seconds: int = 300) -> bytes:
        path = self._cache_path(url)
        if path.exists() and time.time() - path.stat().st_mtime <= max_age_seconds:
            return path.read_bytes()

        delay = self.interval - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Host": urllib.parse.urlparse(url).netloc,
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
        self._last_request = time.monotonic()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return payload

    def get_json(self, url: str, max_age_seconds: int = 300) -> dict[str, Any]:
        return json.loads(self._request(url, max_age_seconds).decode())

    def get_text(self, url: str, max_age_seconds: int = 86_400) -> str:
        return self._request(url, max_age_seconds).decode(errors="replace")

    def ticker_map(self) -> dict[str, str]:
        raw = self.get_json(SEC_TICKERS_URL, max_age_seconds=86_400)
        return {
            str(item["ticker"]).upper(): str(item["cik_str"]).zfill(10) for item in raw.values()
        }

    def submissions(self, cik: str) -> dict[str, Any]:
        return self.get_json(
            f"{SEC_DATA_BASE}/submissions/CIK{str(cik).zfill(10)}.json",
            max_age_seconds=60,
        )

    def company_facts(self, cik: str) -> dict[str, Any]:
        return self.get_json(
            f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{str(cik).zfill(10)}.json",
            max_age_seconds=60,
        )

    def recent_filings(
        self,
        cik: str,
        forms: set[str] | None = None,
        filed_on_or_after: date | None = None,
    ) -> list[SECFiling]:
        padded = str(cik).zfill(10)
        recent = self.submissions(padded)["filings"]["recent"]
        rows: list[SECFiling] = []
        for index, accession in enumerate(recent["accessionNumber"]):
            form = str(recent["form"][index])
            filed_at = date.fromisoformat(str(recent["filingDate"][index]))
            if forms is not None and form not in forms:
                continue
            if filed_on_or_after is not None and filed_at < filed_on_or_after:
                continue
            accepted_raw = str(recent.get("acceptanceDateTime", [""] * len(recent["form"]))[index])
            accepted_at = None
            if accepted_raw:
                accepted_at = _parse_acceptance_datetime(accepted_raw)
            primary = str(recent["primaryDocument"][index])
            accession_compact = str(accession).replace("-", "")
            cik_compact = str(int(padded))
            rows.append(
                SECFiling(
                    cik=padded,
                    accession_number=str(accession),
                    form=form,
                    filed_at=filed_at,
                    accepted_at=accepted_at,
                    primary_document=primary,
                    url=f"{SEC_ARCHIVES_BASE}/{cik_compact}/{accession_compact}/{primary}",
                )
            )
        return rows

    def filing_text(self, filing: SECFiling) -> str:
        return self.get_text(filing.url, max_age_seconds=86_400)

    @staticmethod
    def quote_is_grounded(document: str, quote: str) -> bool:
        normalized_document = normalize_text(document).casefold()
        normalized_quote = normalize_text(quote).casefold()
        return len(normalized_quote) >= 20 and normalized_quote in normalized_document
