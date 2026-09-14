"""HTTP client for Cognee's hosted knowledge-graph API.

Cognee is one of the three sponsor technologies this project has to make load-bearing
(SPEC.md §1), and its pipeline is ``add`` → ``cognify`` → ``search``: you hand it documents,
it extracts an entity/relationship graph from them, and you query that graph in natural
language.

Endpoint shapes
---------------
The paths and payload shapes below are modelled on the **open-source cognee FastAPI server**
(``topoteretes/cognee``), whose routers live under ``/api/v1`` — ``add`` takes multipart form
data with a ``datasetName`` field, ``cognify`` and ``search`` take JSON, and bearer tokens go
in ``Authorization``. The hosted platform mirrors that surface, but a managed API drifts, so:

* every path and payload key is a module-level constant, changeable in one place;
* ``add`` sends multipart and **falls back to JSON** if the server rejects the encoding;
* response parsing accepts several plausible shapes rather than one exact schema;
* every call raises :class:`ProviderUnavailableError` on any failure, so the provider factory
  can fall back to ``LocalGraphMemory`` instead of taking the demo down.

Nothing here retries blindly: a hackathon demo would rather fail over to local in 5 seconds
than hang for 30.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from munshiji.config import get_settings
from munshiji.errors import ProviderUnavailableError
from munshiji.logging import get_logger

__all__ = [
    "ADD_PATH",
    "API_PREFIX",
    "COGNIFY_PATH",
    "DATASETS_PATH",
    "GRAPH_PATH",
    "HEALTH_PATHS",
    "SEARCH_PATH",
    "SEARCH_TYPE_GRAPH_COMPLETION",
    "CogneeAddResult",
    "CogneeClient",
    "CogneeCognifyResult",
    "CogneeDocument",
    "CogneeSearchResult",
]

logger = get_logger(__name__)

# ── Endpoint surface ────────────────────────────────────────────────────────
# Based on the cognee OSS FastAPI app (cognee/api/v1/*), which the hosted platform mirrors.
API_PREFIX = "/api/v1"
ADD_PATH = f"{API_PREFIX}/add"
COGNIFY_PATH = f"{API_PREFIX}/cognify"
SEARCH_PATH = f"{API_PREFIX}/search"
DATASETS_PATH = f"{API_PREFIX}/datasets"
#: ``{dataset_id}`` is the uuid returned by ``GET /datasets`` — not the human dataset name.
GRAPH_PATH = f"{API_PREFIX}/datasets/{{dataset_id}}/graph"
#: Tried in order; older builds expose ``/health``, newer ones put it under the API prefix.
HEALTH_PATHS: tuple[str, ...] = ("/health", f"{API_PREFIX}/health", DATASETS_PATH)

#: Cognee's SearchType enum. GRAPH_COMPLETION answers from the extracted graph rather than
#: raw chunks, which is the behaviour SPEC.md §8 asks for.
SEARCH_TYPE_GRAPH_COMPLETION = "GRAPH_COMPLETION"

#: Form field names used by the multipart ``add`` endpoint.
ADD_FILE_FIELD = "data"
ADD_DATASET_FIELD = "datasetName"

_CONNECT_TIMEOUT = 5.0
_MAX_ERROR_DETAIL = 300

#: Machine-readable header we stamp on every uploaded document so search results can be
#: mapped back onto our own node references.
_REF_RE = re.compile(r"ref=([a-z_]+:[A-Za-z0-9_\-.:]+)")


@dataclass(slots=True)
class CogneeDocument:
    """One fact, formatted for Cognee's extractor."""

    ref: str
    title: str
    text: str

    @property
    def filename(self) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", self.ref)
        return f"{safe}.txt"

    def as_text(self) -> str:
        """Document body with a parseable header line, so results can carry provenance."""
        return f"[[munshiji ref={self.ref} title={self.title}]]\n{self.text}"


@dataclass(slots=True)
class CogneeAddResult:
    dataset: str
    document_count: int
    dataset_id: str | None = None
    raw: Any = None


@dataclass(slots=True)
class CogneeCognifyResult:
    dataset: str
    status: str = "accepted"
    raw: Any = None


@dataclass(slots=True)
class CogneeSearchResult:
    """One search hit, normalised out of whatever shape the API returned."""

    text: str
    score: float = 0.0
    ref: str | None = None
    name: str = ""
    raw: Any = field(default=None, repr=False)


def _clip(value: str, limit: int = _MAX_ERROR_DETAIL) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


class CogneeClient:
    """Thin, defensive async wrapper over the Cognee REST API."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Pass ``transport`` (e.g. ``httpx.MockTransport``) or ``client`` to test offline."""
        settings = get_settings()
        self.api_key = (api_key if api_key is not None else settings.cognee_api_key).strip()
        self.base_url = (base_url or settings.cognee_base_url).rstrip("/")
        self.timeout = float(timeout or settings.http_timeout_seconds)

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=min(_CONNECT_TIMEOUT, self.timeout)),
                headers=self._headers(),
                transport=transport,
            )
            self._owns_client = True

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def aclose(self) -> None:
        """Close the underlying connection pool (only if this client created it)."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> CogneeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ── transport ───────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        tolerate: Sequence[int] = (),
        **kwargs: Any,
    ) -> httpx.Response:
        """Issue one request. Any transport error or unexpected status is fatal-but-catchable.

        ``tolerate`` lists status codes the caller wants to inspect itself (used by ``add``
        to detect an encoding the server dislikes and retry in another form).
        """
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailableError(
                f"cognee {method} {path} timed out after {self.timeout:g}s",
                provider="cognee",
                path=path,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"cognee {method} {path} failed: {type(exc).__name__}: {exc}",
                provider="cognee",
                path=path,
            ) from exc

        if response.status_code in tolerate or response.is_success:
            return response

        raise ProviderUnavailableError(
            f"cognee {method} {path} returned {response.status_code}: " f"{_clip(response.text)}",
            provider="cognee",
            path=path,
            status_code=response.status_code,
        )

    @staticmethod
    def _payload(response: httpx.Response) -> Any:
        """Decode JSON, tolerating servers that answer ``200 text/plain``."""
        try:
            return response.json()
        except ValueError:
            return {"text": response.text}

    # ── pipeline ────────────────────────────────────────────────────────────

    async def add(
        self, documents: Sequence[CogneeDocument], *, dataset_name: str
    ) -> CogneeAddResult:
        """Upload documents into ``dataset_name`` (pipeline step 1 of 3).

        Sent as multipart, matching the OSS ``/add`` signature. If the server answers 4xx in a
        way that means "wrong encoding", the same payload is retried as JSON.
        """
        if not documents:
            return CogneeAddResult(dataset=dataset_name, document_count=0)

        files = [
            (
                ADD_FILE_FIELD,
                (document.filename, document.as_text().encode("utf-8"), "text/plain"),
            )
            for document in documents
        ]
        response = await self._request(
            "POST",
            ADD_PATH,
            files=files,
            data={ADD_DATASET_FIELD: dataset_name},
            tolerate=(400, 415, 422),
        )

        if not response.is_success:
            logger.debug(
                "cognee /add rejected multipart (%s); retrying as JSON", response.status_code
            )
            response = await self._request(
                "POST",
                ADD_PATH,
                json={
                    ADD_FILE_FIELD: [document.as_text() for document in documents],
                    ADD_DATASET_FIELD: dataset_name,
                    "dataset_name": dataset_name,
                },
            )

        payload = self._payload(response)
        return CogneeAddResult(
            dataset=dataset_name,
            document_count=len(documents),
            dataset_id=_first_id(payload),
            raw=payload,
        )

    async def cognify(self, *, dataset_name: str) -> CogneeCognifyResult:
        """Build the knowledge graph over a dataset (pipeline step 2 of 3)."""
        response = await self._request(
            "POST",
            COGNIFY_PATH,
            json={"datasets": [dataset_name], "dataset_name": dataset_name},
        )
        payload = self._payload(response)
        status = "accepted"
        if isinstance(payload, dict):
            status = str(payload.get("status") or payload.get("state") or status)
        return CogneeCognifyResult(dataset=dataset_name, status=status, raw=payload)

    async def search(
        self,
        query: str,
        *,
        dataset_name: str,
        search_type: str = SEARCH_TYPE_GRAPH_COMPLETION,
        top_k: int = 10,
    ) -> list[CogneeSearchResult]:
        """Query the graph (pipeline step 3 of 3). Returns a normalised, possibly empty list."""
        response = await self._request(
            "POST",
            SEARCH_PATH,
            json={
                "query": query,
                # Several releases have used different key names for the same three things.
                "searchType": search_type,
                "search_type": search_type,
                "datasets": [dataset_name],
                "datasetName": dataset_name,
                "top_k": top_k,
            },
        )
        return parse_search_results(self._payload(response), limit=top_k)

    # ── datasets ────────────────────────────────────────────────────────────

    async def datasets(self) -> list[dict[str, Any]]:
        """List datasets visible to this API key."""
        payload = self._payload(await self._request("GET", DATASETS_PATH))
        if isinstance(payload, dict):
            payload = payload.get("datasets") or payload.get("data") or []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    async def dataset_id(self, dataset_name: str) -> str | None:
        """Resolve a dataset name to the uuid the graph endpoint needs. ``None`` if absent."""
        for dataset in await self.datasets():
            name = dataset.get("name") or dataset.get("datasetName") or dataset.get("dataset")
            if name == dataset_name:
                identifier = dataset.get("id") or dataset.get("dataset_id")
                return str(identifier) if identifier is not None else None
        return None

    async def graph(self, *, dataset_name: str) -> Any | None:
        """Best-effort graph export for a dataset; ``None`` when the endpoint is unavailable."""
        identifier = await self.dataset_id(dataset_name)
        if identifier is None:
            return None
        response = await self._request(
            "GET", GRAPH_PATH.format(dataset_id=identifier), tolerate=(404, 501)
        )
        if not response.is_success:
            return None
        return self._payload(response)

    async def delete_dataset(self, dataset_name: str) -> bool:
        """Delete a dataset. Returns ``False`` when it did not exist or cannot be removed."""
        identifier = await self.dataset_id(dataset_name)
        if identifier is None:
            return False
        response = await self._request(
            "DELETE", f"{DATASETS_PATH}/{identifier}", tolerate=(404, 405, 501)
        )
        return bool(response.is_success)

    async def ping(self) -> str:
        """Cheap authenticated probe. Raises :class:`ProviderUnavailableError` if unreachable."""
        last: ProviderUnavailableError | None = None
        for path in HEALTH_PATHS:
            try:
                response = await self._request("GET", path)
            except ProviderUnavailableError as exc:
                last = exc
                continue
            return f"{path} -> {response.status_code}"
        raise last or ProviderUnavailableError("cognee unreachable", provider="cognee")


# ── tolerant response parsing ───────────────────────────────────────────────

_TEXT_KEYS = ("text", "content", "answer", "summary", "description", "value", "chunk", "name")
_SCORE_KEYS = ("score", "similarity", "relevance", "rank")
_NAME_KEYS = ("name", "title", "label", "node_name", "id")
_LIST_KEYS = ("results", "data", "items", "search_results", "documents", "graphs", "answers")


def _first_id(payload: Any) -> str | None:
    """Pull a dataset id out of an ``add`` response, whatever shape it took."""
    if isinstance(payload, dict):
        for key in ("dataset_id", "datasetId", "id"):
            value = payload.get(key)
            if isinstance(value, str | int):
                return str(value)
        for key in _LIST_KEYS:
            nested = payload.get(key)
            if nested is not None:
                return _first_id(nested)
    if isinstance(payload, list) and payload:
        return _first_id(payload[0])
    return None


def _as_items(payload: Any) -> list[Any]:
    """Find the list of results inside whatever envelope the API used."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
            if isinstance(nested, str) and nested.strip():
                return [nested]
        return [payload]
    if isinstance(payload, str):
        return [payload] if payload.strip() else []
    return []


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate
    if isinstance(value, list):
        joined = " ".join(part for part in (_stringify(item) for item in value) if part)
        return joined
    return ""


def parse_search_results(payload: Any, *, limit: int = 10) -> list[CogneeSearchResult]:
    """Normalise a Cognee search response into :class:`CogneeSearchResult` objects.

    GRAPH_COMPLETION has variously returned a bare list of strings, a list of
    ``{"text": …, "score": …}`` objects, and an envelope with a ``results`` key. All three are
    accepted; anything unrecognised yields an empty list rather than an exception.
    """
    results: list[CogneeSearchResult] = []
    for item in _as_items(payload):
        if isinstance(item, str):
            text = item
            score, name, raw = 0.0, "", item
        elif isinstance(item, dict):
            text = _stringify(item)
            score = 0.0
            for key in _SCORE_KEYS:
                value = item.get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    score = float(value)
                    break
            name = ""
            for key in _NAME_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
            raw = item
        else:
            continue
        if not text.strip():
            continue
        match = _REF_RE.search(text)
        results.append(
            CogneeSearchResult(
                text=text.strip(),
                score=score,
                ref=match.group(1) if match else None,
                name=name,
                raw=raw,
            )
        )
        if len(results) >= limit:
            break
    return results
