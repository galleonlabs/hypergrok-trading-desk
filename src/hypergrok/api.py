"""Small, explicit HTTP clients for desk research."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from . import __version__

MAX_RESPONSE_BYTES = 10 * 1024 * 1024

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
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ApiError("Only absolute HTTPS API URLs are allowed")
    body = json.dumps(payload).encode() if payload is not None else None
    merged = {"Accept": "application/json", "User-Agent": f"HyperGrok/{__version__}"}
    if payload is not None:
        merged["Content-Type"] = "application/json"
    if headers:
        merged.update(headers)
    request = Request(url, data=body, headers=merged, method=method)
    try:
        # The URL scheme and host are constrained above.
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ApiError(f"Response from {url} exceeded {MAX_RESPONSE_BYTES} bytes")
            return json.loads(raw)
    except HTTPError as exc:
        origin = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
        raise ApiError(f"HTTP {exc.code} from {origin}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ApiError(f"Request failed for {url}: {exc}") from exc


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
