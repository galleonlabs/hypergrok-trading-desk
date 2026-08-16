"""Small, explicit HTTP clients for desk research."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    """A remote API failed or returned unusable data."""


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20,
) -> Any:
    body = json.dumps(payload).encode() if payload is not None else None
    merged = {"Accept": "application/json", "User-Agent": "HyperGrok/0.1"}
    if payload is not None:
        merged["Content-Type"] = "application/json"
    if headers:
        merged.update(headers)
    request = Request(url, data=body, headers=merged, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise ApiError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(f"Request failed for {url}: {exc}") from exc


def hyperliquid_info(kind: str, **params: Any) -> Any:
    return request_json(
        "https://api.hyperliquid.xyz/info",
        method="POST",
        payload={"type": kind, **params},
    )


def defillama_protocol(slug: str) -> Any:
    return request_json(f"https://api.llama.fi/protocol/{quote(slug, safe='')}")


def coingecko_coin(coin_id: str, api_key: str | None = None, pro: bool = False) -> Any:
    base = "https://pro-api.coingecko.com/api/v3" if pro else "https://api.coingecko.com/api/v3"
    headers: dict[str, str] = {}
    if api_key:
        headers["x-cg-pro-api-key" if pro else "x-cg-demo-api-key"] = api_key
    return request_json(
        f"{base}/coins/{quote(coin_id, safe='')}?localization=false&tickers=false"
        "&market_data=true&community_data=false&developer_data=false",
        headers=headers,
    )
