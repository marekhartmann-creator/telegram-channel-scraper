"""Apify entry point.

Contract with the user:
  * every post pushed to the dataset was verified to come from a channel we
    could actually read;
  * a channel we could not read produces an explicit error code, never an
    empty result;
  * charging happens per emitted post, so a run that returns nothing costs
    nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from apify import Actor

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tg.fetch import TelegramClient  # noqa: E402
from tg.scraper import (  # noqa: E402
    ScrapeOptions,
    normalize_handle,
    parse_iso,
    scrape_channels,
)

CHARGE_EVENT_POST = "post-scraped"


async def _charge(count: int) -> None:
    """Charge for verified posts only. Silently no-op when PPE is not enabled."""
    if count <= 0:
        return
    try:
        await Actor.charge(event_name=CHARGE_EVENT_POST, count=count)
    except Exception as exc:  # noqa: BLE001
        Actor.log.debug(f"Charging skipped ({exc}).")


async def main() -> None:
    async with Actor:
        raw_input: dict[str, Any] = await Actor.get_input() or {}

        raw_channels = raw_input.get("channels") or []
        if isinstance(raw_channels, str):
            raw_channels = [raw_channels]

        handles: list[str] = []
        rejected: list[str] = []
        for entry in raw_channels:
            value = entry.get("url") if isinstance(entry, dict) else entry
            handle = normalize_handle(str(value or ""))
            if handle and handle not in handles:
                handles.append(handle)
            elif value:
                rejected.append(str(value))

        if not handles:
            await Actor.fail(
                status_message=(
                    "No usable channel handle in the input. "
                    f"Rejected: {rejected or 'nothing supplied'}. "
                    "Give a public channel handle, e.g. 'durov' or 'https://t.me/durov'."
                )
            )
            return

        options = ScrapeOptions(
            max_posts=int(raw_input.get("maxPostsPerChannel") or 0),
            min_date=parse_iso(raw_input.get("minDate")),
            max_date=parse_iso(raw_input.get("maxDate")),
            search_terms=tuple(raw_input.get("searchTerms") or ()),
            include_media=bool(raw_input.get("includeMediaUrls", True)),
            include_html=bool(raw_input.get("includeHtml", False)),
            concurrency=max(1, min(10, int(raw_input.get("maxConcurrency") or 5))),
        )
        fail_on_unreadable = bool(raw_input.get("failOnUnreadableChannel", True))

        # Proxy is a nice-to-have, never a reason to kill the run: without an
        # Apify Proxy password (local runs, plans without proxy access) the SDK
        # raises, and t.me is perfectly reachable directly.
        proxy_url: str | None = None
        proxy_cfg = None
        try:
            proxy_cfg = await Actor.create_proxy_configuration(
                actor_proxy_input=raw_input.get("proxyConfiguration")
            )
            if proxy_cfg:
                proxy_url = await proxy_cfg.new_url()
        except Exception as exc:  # noqa: BLE001
            proxy_cfg = None
            Actor.log.warning(f"Proxy unavailable, continuing without it ({exc}).")

        async def dalsia_proxy() -> str | None:
            """A6-V7 (R-F74AC6): nova vystupna adresa na poziadanie.

            Predtym sa new_url() volalo RAZ pred vytvorenim klienta, takze
            cely beh sedel na jednej adrese - ked ju t.me zablokovalo,
            zlyhali vsetky dalsie pokusy rovnako.
            """
            if proxy_cfg is None:
                return None
            try:
                nova = await proxy_cfg.new_url()
            except Exception as exc:  # noqa: BLE001
                Actor.log.warning(f"Proxy rotation failed, continuing ({exc}).")
                return None
            Actor.log.info("Rotated to a new proxy address.")
            return nova

        reports: list[dict[str, Any]] = []
        total_posts = 0

        async def push(batch: list[dict[str, Any]]) -> None:
            nonlocal total_posts
            await Actor.push_data(batch)
            await _charge(len(batch))
            total_posts += len(batch)

        async def announce(handle: str, report) -> None:
            if report.verdict == "OK":
                Actor.log.info(f"{handle}: {report.message}")
            elif report.verdict == "EMPTY_VERIFIED":
                Actor.log.warning(f"{handle}: {report.message}")
            else:
                Actor.log.error(f"{handle}: [{report.state.value}] {report.message}")

        async with TelegramClient(
            proxy_url=proxy_url, proxy_provider=dalsia_proxy
        ) as client:
            Actor.log.info(f"Scraping {len(handles)} channel(s): {', '.join(handles)}")
            results = await scrape_channels(client, handles, options, push, announce)
            reports = [report.to_item() for _, report in results]

        summary = {
            "_type": "runSummary",
            "channelsRequested": len(handles),
            "channelsOk": sum(1 for r in reports if r["verdict"] in ("OK", "EMPTY_VERIFIED")),
            "channelsFailed": sum(1 for r in reports if r["verdict"] == "FAILED"),
            "channelsPartial": sum(1 for r in reports if r["verdict"] == "PARTIAL"),
            "postsPushed": total_posts,
            "rejectedInputs": rejected,
            "reports": reports,
        }
        # The verification reports live in the key-value store, not in the
        # dataset: the dataset stays one clean row per post, which is also the
        # only thing the customer is charged for.
        await Actor.set_value("RUN_SUMMARY", summary, content_type="application/json")

        broken = [r for r in reports if r["verdict"] in ("FAILED", "PARTIAL")]
        if broken and fail_on_unreadable:
            lines = "; ".join(f"{r['handle']}: {r['state']} - {r['message']}" for r in broken)
            await Actor.fail(
                status_message=(
                    f"{len(broken)} of {len(handles)} channel(s) could not be fully read. "
                    f"{lines} "
                    "Set failOnUnreadableChannel=false to accept partial results."
                )
            )
            return

        Actor.log.info(
            f"Done. {total_posts} verified posts from "
            f"{summary['channelsOk']}/{len(handles)} channels."
        )
