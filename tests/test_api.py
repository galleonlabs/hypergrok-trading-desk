import json
from io import BytesIO
from urllib.request import Request

import pytest

from hypergrok import api


class Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def test_request_json_sets_version_and_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def fake_open(request: Request, timeout: float):
        observed["request"] = request
        observed["timeout"] = timeout
        return Response(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(api, "urlopen", fake_open)
    assert api.request_json("https://example.test") == {"ok": True}
    request = observed["request"]
    assert isinstance(request, Request)
    assert request.get_header("User-agent") == "HyperGrok/1.0.0"


def test_request_json_rejects_oversized_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        api,
        "urlopen",
        lambda request, timeout: Response(b"x" * (api.MAX_RESPONSE_BYTES + 1)),
    )
    with pytest.raises(api.ApiError, match="exceeded"):
        api.request_json("https://example.test")


def test_request_json_rejects_non_https_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "urlopen", lambda *args, **kwargs: pytest.fail("network called"))
    with pytest.raises(api.ApiError, match="HTTPS"):
        api.request_json("file:///etc/passwd")
