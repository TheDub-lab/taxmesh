"""HTTP client for the Atlas tax engine (API side of the billing integration)."""

from __future__ import annotations

import httpx


class EngineError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(f"[{status_code}] {code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class EngineClient:
    """Wraps an httpx client; injectable so tests can run the engine in-process."""

    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        if client is not None:
            self._client = client
        else:
            if base_url is None:
                raise ValueError("EngineClient needs a base_url or an injected client")
            self._client = httpx.Client(base_url=base_url, timeout=5.0)

    def close(self) -> None:
        self._client.close()

    def _handle(self, resp: httpx.Response) -> dict:
        if resp.status_code == 422:
            detail = resp.json().get("detail", {})
            raise EngineError(422, detail.get("code", "VALIDATION"),
                              detail.get("message", str(detail)))
        resp.raise_for_status()
        return resp.json()

    def health(self) -> dict:
        return self._handle(self._client.get("/v2/health"))

    def estimate(self, payload: dict) -> dict:
        return self._handle(self._client.post("/v2/estimate", json=payload))

    def commit(self, payload: dict) -> dict:
        return self._handle(self._client.post("/v2/commit", json=payload))

    def transactions(self) -> list[dict]:
        return self._handle(self._client.get("/v2/transactions"))["transactions"]

    def validate_cert(self, payload: dict) -> dict:
        return self._handle(self._client.post("/v2/certs/validate", json=payload))
