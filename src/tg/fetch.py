"""HTTP layer.

Plain httpx against the server-rendered preview. No browser: a headless Chrome
would multiply this Actor's memory footprint (and therefore the customer's bill)
for pages that are static HTML anyway.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

BASE = "https://t.me"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class FetchResult:
    """One HTTP attempt, success or not — never silently collapsed to ''."""

    url: str
    status: int | None = None
    html: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)


@dataclass
class FetchStats:
    requests: int = 0
    retries: int = 0
    failures: list[str] = field(default_factory=list)



# A3-V6: Retry-After je POKYN servera, nie navrh.
#
# Backoff nizsie mal nahodnu zlozku (dobre), ale hlavicku vobec necital (zle):
# pri strope 8 s sme vycerpali vsetky tri pokusy za ~20 s aj vtedy, ked server
# povedal "pridi o 60 s". Vsetky tri pokusy tak padli do okna, ktore uz bolo
# zavrete, a beh skoncil ako zlyhanie - hoci stacilo pockat.
RETRY_AFTER_MAX_S = 120.0


def _retry_after_seconds(headers: Any) -> float | None:
    """Retry-After v sekundach. Podporuje aj tvar s datumom (HTTP-date)."""
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        at = parsedate_to_datetime(str(raw))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return max(0.0, (at - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return None


class TelegramClient:
    """Retrying fetcher for t.me pages."""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        min_delay: float = 0.25,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            timeout=timeout,
            follow_redirects=True,
            proxy=proxy_url,
        )
        self.max_retries = max_retries
        self.min_delay = min_delay
        self.stats = FetchStats()

    async def __aenter__(self) -> "TelegramClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Ako dlho pockat pred dalsim pokusom.

        Ked server posle Retry-After, je to SPODNA hranica - nikdy sa nezobudime
        skor, nanajvys o nahodnu chvilu neskor. Bez hlavicky ostava povodny
        rastuci odstup s nahodnou zlozkou (aby sa suborne behy nezobudili naraz).
        """
        if retry_after is not None:
            return min(retry_after + random.random(), RETRY_AFTER_MAX_S)
        return min(8.0, 2**attempt) + random.random()

    async def get(self, url: str) -> FetchResult:
        last = FetchResult(url=url)
        retry_after: float | None = None
        for attempt in range(self.max_retries):
            if attempt:
                self.stats.retries += 1
                await asyncio.sleep(self._backoff(attempt, retry_after))
            retry_after = None
            try:
                self.stats.requests += 1
                response = await self._client.get(url)
            except Exception as exc:  # noqa: BLE001 - reported, never swallowed
                last = FetchResult(url=url, error=f"{type(exc).__name__}: {exc}")
                continue

            if response.status_code == 200:
                await asyncio.sleep(self.min_delay)
                return FetchResult(url=url, status=200, html=response.text)

            last = FetchResult(
                url=url,
                status=response.status_code,
                error=f"HTTP {response.status_code}",
            )
            retry_after = _retry_after_seconds(response.headers)
            # 4xx other than rate limiting will not improve on retry.
            if response.status_code < 500 and response.status_code != 429:
                break

        if last.error:
            self.stats.failures.append(f"{url} -> {last.error}")
        return last

    async def preview_page(self, handle: str, before: int | None = None) -> FetchResult:
        url = f"{BASE}/s/{handle}"
        if before is not None:
            url = f"{url}?before={before}"
        return await self.get(url)

    async def plain_page(self, handle: str) -> FetchResult:
        return await self.get(f"{BASE}/{handle}")
