"""Клиент deltaforceapi.com (Connect-RPC поверх HTTP POST + JSON)."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from config import API_BASE, API_POLITE_DELAY

_client = httpx.AsyncClient(timeout=20, headers={"Content-Type": "application/json"})


async def _call(method: str, payload: dict) -> dict:
    r = await _client.post(f"{API_BASE}/{method}", json=payload)
    r.raise_for_status()
    return r.json()


async def list_all_items() -> list[dict]:
    """Все предметы аукциона: [{id, name, iconUrl}, ...] (с пагинацией)."""
    items, token = [], None
    while True:
        payload = {"pageSize": 100}
        if token:
            payload["pageToken"] = token
        data = await _call("ListAuctionItems", payload)
        items.extend(data.get("items", []))
        token = data.get("nextPageToken")
        if not token:
            break
        await asyncio.sleep(API_POLITE_DELAY)
    return items


async def get_price(item_id: str) -> dict | None:
    """{price, referencePrice, createdAt} или None."""
    try:
        data = await _call("GetAuctionItemPrice", {"auctionItemId": item_id})
        return data.get("price")
    except httpx.HTTPStatusError:
        return None


async def get_series(item_id: str, days: int = 7) -> list[dict]:
    """Почасовая история: [{timestamp, priceLow, priceHigh, priceAvg}, ...]."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:00:00Z")
    try:
        data = await _call("GetAuctionItemPriceSeries", {"auctionItemId": item_id, "startTime": start})
        return data.get("prices", [])
    except httpx.HTTPStatusError:
        return []


async def get_current_season() -> dict | None:
    try:
        data = await _call("GetSeasonCurrent", {})
        return data.get("season")
    except httpx.HTTPStatusError:
        return None
