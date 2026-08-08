from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import math
import os
import re
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


_SCHEMA_VERSION = 1
_MAX_QUERY_CHARS = 512
_MAX_RESULTS = 10
_MAX_PROVIDER_CANDIDATES = 40
_MAX_TITLE_CHARS = 200
_MAX_SNIPPET_CHARS = 800
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_SAVED_RESPONSE_BYTES = 128_000
_MAX_CACHE_BYTES = 128_000
_MAX_PUBLIC_TOPICS = 6
_MAX_PUBLIC_TOPIC_CHARS = 80
_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MARKDOWN_ANGLE_DESTINATION_RE = re.compile(r"[<>]")
_TAG_RE = re.compile(r"<[^>]+>")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_PUBLIC_TOPIC_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#'.-]*")


class ResearchError(RuntimeError):
    """Base error for the model-free web-discovery boundary."""


class ResearchValidationError(ResearchError):
    """A request or provider result violated the bounded research contract."""


class ResearchTransportError(ResearchError):
    """A redacted live-provider failure."""


@dataclass(frozen=True)
class ResearchRequest:
    query: str
    max_results: int = 5

    def __post_init__(self) -> None:
        query = " ".join(str(self.query).split())
        if not query:
            raise ResearchValidationError("Research query cannot be empty.")
        if len(query) > _MAX_QUERY_CHARS:
            raise ResearchValidationError(
                f"Research query cannot exceed {_MAX_QUERY_CHARS} characters."
            )
        if isinstance(self.max_results, bool) or not isinstance(self.max_results, int):
            raise ResearchValidationError("max_results must be an integer.")
        if not 1 <= self.max_results <= _MAX_RESULTS:
            raise ResearchValidationError(
                f"max_results must be between 1 and {_MAX_RESULTS}."
            )
        object.__setattr__(self, "query", query)

    @property
    def sha256(self) -> str:
        """Stable content-free identifier used to bind saved replay to a request."""
        return _request_hash(self)


def build_public_concept_query(concepts: Sequence[str]) -> str:
    """Build one bounded query only from explicitly trusted public topic names."""
    if isinstance(concepts, (str, bytes)) or not 1 <= len(concepts) <= _MAX_PUBLIC_TOPICS:
        raise ResearchValidationError(
            f"Public research topics must contain between 1 and {_MAX_PUBLIC_TOPICS} items."
        )
    cleaned: list[str] = []
    normalized: set[str] = set()
    for value in concepts:
        if not isinstance(value, str):
            raise ResearchValidationError("Public research topics must be strings.")
        topic = " ".join(value.split())
        if not topic or len(topic) > _MAX_PUBLIC_TOPIC_CHARS:
            raise ResearchValidationError(
                f"Each public research topic must be 1-{_MAX_PUBLIC_TOPIC_CHARS} characters."
            )
        if "://" in topic or "@" in topic or _CONTROL_RE.search(topic):
            raise ResearchValidationError(
                "Public research topics cannot contain URLs, email addresses, or controls."
            )
        public_words = " ".join(_PUBLIC_TOPIC_TOKEN_RE.findall(topic))
        if not public_words:
            raise ResearchValidationError(
                "Public research topics must contain ordinary topic words."
            )
        key = public_words.casefold()
        if key in normalized:
            raise ResearchValidationError("Public research topics cannot repeat.")
        normalized.add(key)
        cleaned.append(public_words)
    query = "(" + " OR ".join(f'"{topic}"' for topic in cleaned) + ") educational reference tutorial"
    return ResearchRequest(query).query


@dataclass(frozen=True)
class ResearchCandidate:
    """Untrusted provider output; only ResearchEngine may promote it to a hit."""

    title: str
    url: str
    snippet: str = ""


@dataclass(frozen=True)
class ResearchHit:
    title: str
    url: str
    snippet: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


@dataclass(frozen=True)
class AdapterReply:
    candidates: tuple[ResearchCandidate, ...]
    provider_requests: int
    estimated_cost_usd: float | None = None
    retrieved_at: datetime | None = None


@dataclass(frozen=True)
class ResearchUsage:
    provider: str
    source: str
    provider_requests: int
    estimated_cost_usd: float | None
    model_attempted: bool = False
    input_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "provider_requests": self.provider_requests,
            "estimated_cost_usd": self.estimated_cost_usd,
            "model_attempted": self.model_attempted,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


@dataclass(frozen=True)
class ResearchOutcome:
    request_sha256: str
    hits: tuple[ResearchHit, ...]
    dropped_hits: int
    retrieved_at: datetime
    usage: ResearchUsage

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_sha256": self.request_sha256,
            "result_count": len(self.hits),
            "dropped_hits": self.dropped_hits,
            "retrieved_at": self.retrieved_at.isoformat(timespec="seconds"),
            "hits": [hit.to_dict() for hit in self.hits],
            "usage": self.usage.to_dict(),
        }


class ResearchAdapter(Protocol):
    """Small transport seam; normalization, budgets, and cache stay in the engine."""

    name: str
    source: str
    cacheable: bool
    cache_namespace: str

    def fetch(self, request: ResearchRequest) -> AdapterReply: ...


class SavedJsonResearchAdapter:
    """Deterministic normalized-result replay with no network or model usage."""

    name = "saved-json"
    source = "replay"
    cacheable = False

    def __init__(self, response_path: Path):
        self.response_path = Path(response_path)
        try:
            if self.response_path.stat().st_size > _MAX_SAVED_RESPONSE_BYTES:
                raise ResearchValidationError(
                    "Saved research response exceeds the size limit."
                )
            body = self.response_path.read_bytes()
        except ResearchValidationError:
            raise
        except OSError as exc:
            raise ResearchValidationError("Saved research response cannot be read.") from exc
        if len(body) > _MAX_SAVED_RESPONSE_BYTES:
            raise ResearchValidationError("Saved research response exceeds the size limit.")
        digest = hashlib.sha256(body).hexdigest()
        self.cache_namespace = f"saved-json:{digest}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ResearchValidationError(
                "Saved research response must be valid UTF-8 JSON."
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ResearchValidationError(
                f"Saved research response must use schema_version {_SCHEMA_VERSION}."
            )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ResearchValidationError("Saved research response requires a results list.")
        request_sha256 = str(payload.get("request_sha256") or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", request_sha256):
            raise ResearchValidationError(
                "Saved research response requires a valid request_sha256."
            )
        retrieved_at = _parse_aware_datetime(payload.get("retrieved_at"))
        self._request_sha256 = request_sha256
        self._retrieved_at = retrieved_at
        self._candidates = tuple(
            _candidate_from_mapping(item) if isinstance(item, dict) else _invalid_candidate()
            for item in raw_results[:_MAX_PROVIDER_CANDIDATES]
        )

    def fetch(self, request: ResearchRequest) -> AdapterReply:
        if request.sha256 != self._request_sha256:
            raise ResearchValidationError(
                "Saved research response does not match this research request."
            )
        return AdapterReply(
            self._candidates,
            provider_requests=0,
            estimated_cost_usd=0.0,
            retrieved_at=self._retrieved_at,
        )


class SearXNGHttpAdapter:
    """Live adapter for a specifically configured, trusted SearXNG instance."""

    name = "searxng"
    source = "live"
    cacheable = True

    def __init__(
        self,
        base_url: str,
        *,
        timeout: tuple[float, float] = (5.0, 20.0),
        cost_per_request_usd: float | None = None,
        http_get: Callable[..., Any] | None = None,
    ):
        self.endpoint = _searxng_endpoint(base_url)
        self.timeout = timeout
        self.cost_per_request_usd = _validate_cost(cost_per_request_usd)
        self._http_get = http_get or requests.get
        endpoint_hash = hashlib.sha256(self.endpoint.encode("utf-8")).hexdigest()
        self.cache_namespace = f"searxng:{endpoint_hash}"

    def fetch(self, request: ResearchRequest) -> AdapterReply:
        response: Any | None = None
        try:
            response = self._http_get(
                self.endpoint,
                params={
                    "q": request.query,
                    "format": "json",
                    "safesearch": 1,
                },
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status != 200:
                raise ResearchTransportError(
                    f"SearXNG search failed with HTTP {status or 'unknown'}."
                )
            body = _bounded_response_body(response)
        except ResearchTransportError:
            raise
        except Exception:
            raise ResearchTransportError(
                "SearXNG search request or response stream failed."
            ) from None
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ResearchTransportError(
                "SearXNG did not return a valid JSON search response."
            ) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ResearchTransportError(
                "SearXNG JSON response did not contain a results list."
            )
        candidates = tuple(
            (
                ResearchCandidate(
                    title=str(item.get("title") or ""),
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("content") or ""),
                )
                if isinstance(item, dict)
                else _invalid_candidate()
            )
            for item in payload["results"][:_MAX_PROVIDER_CANDIDATES]
        )
        return AdapterReply(
            candidates,
            provider_requests=1,
            estimated_cost_usd=self.cost_per_request_usd,
        )


class ResearchEngine:
    """Validate, deduplicate, cache, and meter one model-free discovery request."""

    def __init__(
        self,
        adapter: ResearchAdapter,
        *,
        cache_dir: Path | None = None,
        cache_ttl: timedelta = timedelta(days=7),
        clock: Callable[[], datetime] | None = None,
    ):
        if cache_ttl.total_seconds() <= 0:
            raise ResearchValidationError("cache_ttl must be positive.")
        self.adapter = adapter
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.cache_ttl = cache_ttl
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def search(self, request: ResearchRequest, *, refresh: bool = False) -> ResearchOutcome:
        request_sha256 = request.sha256
        cache_key = _cache_key(self.adapter.cache_namespace, request)
        if self.adapter.cacheable and self.cache_dir is not None and not refresh:
            cached = self._read_cache(cache_key, request)
            if cached is not None:
                hits, dropped_hits, retrieved_at = cached
                return ResearchOutcome(
                    request_sha256,
                    hits,
                    dropped_hits,
                    retrieved_at,
                    ResearchUsage(
                        provider=self.adapter.name,
                        source="cache",
                        provider_requests=0,
                        estimated_cost_usd=0.0,
                    ),
                )

        reply = self.adapter.fetch(request)
        estimated_cost_usd = _validate_reply_usage(reply)
        hits, dropped_hits = _normalize_candidates(reply.candidates, request.max_results)
        retrieved_at = _normalize_retrieved_at(reply.retrieved_at or self._clock())
        outcome = ResearchOutcome(
            request_sha256,
            hits,
            dropped_hits,
            retrieved_at,
            ResearchUsage(
                provider=self.adapter.name,
                source=self.adapter.source,
                provider_requests=reply.provider_requests,
                estimated_cost_usd=estimated_cost_usd,
            ),
        )
        if self.adapter.cacheable and self.cache_dir is not None:
            self._write_cache(cache_key, outcome)
        return outcome

    def _cache_path(self, cache_key: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / f"{cache_key}.json"

    def _read_cache(
        self, cache_key: str, request: ResearchRequest
    ) -> tuple[tuple[ResearchHit, ...], int, datetime] | None:
        path = self._cache_path(cache_key)
        try:
            if not path.is_file() or path.stat().st_size > _MAX_CACHE_BYTES:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if payload.get("schema_version") != _SCHEMA_VERSION:
                return None
            if payload.get("provider") != self.adapter.name:
                return None
            created_at = _parse_aware_datetime(payload["created_at"])
            retrieved_at = _parse_aware_datetime(payload["retrieved_at"])
            now = _normalize_retrieved_at(self._clock())
            if created_at > now + timedelta(minutes=5):
                return None
            if now - created_at > self.cache_ttl:
                return None
            raw_hits = payload.get("hits")
            if not isinstance(raw_hits, list) or len(raw_hits) > request.max_results:
                return None
            hits = tuple(
                _normalize_candidate(_candidate_from_mapping(item))
                for item in raw_hits
                if isinstance(item, dict)
            )
            if len(hits) != len(raw_hits) or len({hit.url for hit in hits}) != len(hits):
                return None
            dropped_hits = int(payload.get("dropped_hits", 0))
            if dropped_hits < 0:
                return None
            return hits, dropped_hits, retrieved_at
        except (
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
            ResearchValidationError,
        ):
            return None

    def _write_cache(self, cache_key: str, outcome: ResearchOutcome) -> None:
        assert self.cache_dir is not None
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "created_at": _normalize_retrieved_at(self._clock()).isoformat(
                timespec="seconds"
            ),
            "retrieved_at": outcome.retrieved_at.isoformat(timespec="seconds"),
            "provider": self.adapter.name,
            "dropped_hits": outcome.dropped_hits,
            "hits": [hit.to_dict() for hit in outcome.hits],
        }
        temporary: Path | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=".research-", suffix=".tmp", dir=self.cache_dir
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self._cache_path(cache_key))
            temporary = None
        except OSError:
            return
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def default_research_cache_path() -> Path:
    configured = os.environ.get("RESEARCH_CACHE_PATH", "").strip()
    if configured:
        return Path(configured)
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "canvas-obsidian-sync" / "research-cache"
    return Path.home() / ".cache" / "canvas-obsidian-sync" / "research-cache"


def validate_research_cache_path(
    cache_path: Path, *, protected_roots: tuple[Path, ...]
) -> Path:
    """Resolve a cache location and keep search evidence out of protected trees."""
    candidate = Path(cache_path).expanduser()
    if not candidate.is_absolute():
        raise ResearchValidationError("RESEARCH_CACHE_PATH must be absolute.")
    resolved = candidate.resolve(strict=False)
    for root in protected_roots:
        protected = Path(root).resolve(strict=False)
        if resolved == protected or protected in resolved.parents:
            raise ResearchValidationError(
                "Research cache must stay outside the code repository and vault."
            )
    return resolved


def research_adapter_from_env(response_file: Path | None = None) -> ResearchAdapter:
    if response_file is not None:
        return SavedJsonResearchAdapter(response_file)
    provider = os.environ.get("RESEARCH_PROVIDER", "").strip().lower()
    if provider != "searxng":
        raise RuntimeError(
            "Live research is opt-in. Set RESEARCH_PROVIDER=searxng and "
            "SEARXNG_BASE_URL, or pass --response-file for zero-cost replay."
        )
    base_url = os.environ.get("SEARXNG_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("SEARXNG_BASE_URL is required for RESEARCH_PROVIDER=searxng.")
    raw_cost = os.environ.get("RESEARCH_COST_PER_REQUEST_USD", "").strip()
    try:
        cost = float(raw_cost) if raw_cost else None
    except ValueError:
        raise RuntimeError("RESEARCH_COST_PER_REQUEST_USD must be a number.") from None
    return SearXNGHttpAdapter(base_url, cost_per_request_usd=cost)


def _candidate_from_mapping(item: dict[str, Any]) -> ResearchCandidate:
    return ResearchCandidate(
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        snippet=str(item.get("snippet") or ""),
    )


def _invalid_candidate() -> ResearchCandidate:
    return ResearchCandidate(title="", url="", snippet="")


def _clean_text(value: str, limit: int) -> str:
    cleaned = str(value)
    # Decode before stripping so an encoded tag cannot become active markup in
    # the normalized output. Three bounded passes also handle common double
    # encoding without creating an unbounded entity-expansion loop.
    for _ in range(3):
        decoded = html.unescape(cleaned)
        if decoded == cleaned:
            break
        cleaned = decoded
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = _CONTROL_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())[:limit].rstrip()


def _normalize_candidate(candidate: ResearchCandidate) -> ResearchHit:
    title = _clean_text(candidate.title, _MAX_TITLE_CHARS)
    if not title:
        raise ResearchValidationError("Research result title cannot be empty.")
    snippet = _clean_text(candidate.snippet, _MAX_SNIPPET_CHARS)
    return ResearchHit(title, _normalize_public_url(candidate.url), snippet)


def _normalize_candidates(
    candidates: tuple[ResearchCandidate, ...], max_results: int
) -> tuple[tuple[ResearchHit, ...], int]:
    hits: list[ResearchHit] = []
    seen: set[str] = set()
    dropped = max(0, len(candidates) - _MAX_PROVIDER_CANDIDATES)
    for candidate in candidates[:_MAX_PROVIDER_CANDIDATES]:
        try:
            hit = _normalize_candidate(candidate)
        except (ResearchValidationError, TypeError, ValueError):
            dropped += 1
            continue
        if hit.url in seen or len(hits) >= max_results:
            dropped += 1
            continue
        seen.add(hit.url)
        hits.append(hit)
    return tuple(hits), dropped


def _normalize_public_url(value: str) -> str:
    raw_url = str(value).strip()
    if _CONTROL_RE.search(raw_url):
        raise ResearchValidationError("Research result URL contains control characters.")
    # Helpful Links renders normalized URLs as angle-bracket Markdown link
    # destinations. A literal '<' or '>' can terminate or reshape that
    # destination and inject provider-controlled Markdown into the vault.
    # Percent-encoded forms remain safe because they contain no Markdown
    # delimiter and preserve the URL's intended octet on navigation.
    if _MARKDOWN_ANGLE_DESTINATION_RE.search(raw_url):
        raise ResearchValidationError(
            "Research result URL contains unsafe Markdown delimiters."
        )
    try:
        parts = urlsplit(raw_url)
        port = parts.port
    except ValueError as exc:
        raise ResearchValidationError("Research result URL is invalid.") from exc
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ResearchValidationError("Research result URLs must use HTTPS.")
    if parts.username is not None or parts.password is not None:
        raise ResearchValidationError("Research result URLs cannot contain credentials.")
    raw_host = parts.hostname.rstrip(".").lower()
    try:
        host = raw_host.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError as exc:
        raise ResearchValidationError("Research result URL host is invalid.") from exc
    if not host or host == "localhost" or host.endswith(".local"):
        raise ResearchValidationError("Research result URL host is not public.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            socket.inet_aton(host)
        except OSError:
            pass
        else:
            raise ResearchValidationError(
                "Research result URL uses a noncanonical numeric host."
            )
        _validate_dns_host(host)
    else:
        if not address.is_global:
            raise ResearchValidationError("Research result URL host is not public.")
        host = address.compressed
    host_for_url = f"[{host}]" if ":" in host else host
    netloc = host_for_url if port in {None, 443} else f"{host_for_url}:{port}"
    query_items = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit(
        ("https", netloc, parts.path or "/", urlencode(query_items, doseq=True), "")
    )


def _validate_dns_host(host: str) -> None:
    if len(host) > 253:
        raise ResearchValidationError("Research result URL host is invalid.")
    labels = host.split(".")
    if not labels or any(_HOST_LABEL_RE.fullmatch(label) is None for label in labels):
        raise ResearchValidationError("Research result URL host is invalid.")


def _request_hash(request: ResearchRequest) -> str:
    encoded = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "query": request.query,
            "max_results": request.max_results,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cache_key(cache_namespace: str, request: ResearchRequest) -> str:
    encoded = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "adapter": cache_namespace,
            "request_sha256": request.sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_reply_usage(reply: AdapterReply) -> float | None:
    if (
        isinstance(reply.provider_requests, bool)
        or not isinstance(reply.provider_requests, int)
        or reply.provider_requests < 0
        or reply.provider_requests > 10
    ):
        raise ResearchValidationError("Provider request count is invalid.")
    return _validate_cost(reply.estimated_cost_usd)


def _parse_aware_datetime(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ResearchValidationError(
            "Research retrieval timestamp must be valid ISO 8601."
        ) from None
    return _normalize_retrieved_at(parsed)


def _normalize_retrieved_at(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ResearchValidationError("Research retrieval timestamp must include a timezone.")
    return value.astimezone(timezone.utc)


def _validate_cost(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError) as exc:
        raise ResearchValidationError("Estimated research cost must be numeric.") from exc
    if not math.isfinite(cost) or cost < 0:
        raise ResearchValidationError("Estimated research cost cannot be negative.")
    return cost


def _searxng_endpoint(base_url: str) -> str:
    try:
        parts = urlsplit(str(base_url).strip())
        port = parts.port
    except ValueError as exc:
        raise ResearchValidationError("SEARXNG_BASE_URL is invalid.") from exc
    if not parts.hostname or parts.username is not None or parts.password is not None:
        raise ResearchValidationError(
            "SEARXNG_BASE_URL requires a host and cannot contain credentials."
        )
    if parts.query or parts.fragment:
        raise ResearchValidationError(
            "SEARXNG_BASE_URL cannot contain a query string or fragment."
        )
    host = parts.hostname.lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    scheme = parts.scheme.lower()
    if scheme != "https" and not (scheme == "http" and is_loopback):
        raise ResearchValidationError(
            "SEARXNG_BASE_URL must use HTTPS, except for an exact loopback host."
        )
    netloc = parts.hostname
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    if not path.endswith("/search"):
        path = f"{path}/search"
    return urlunsplit((scheme, netloc, path, "", ""))


def _bounded_response_body(response: Any) -> bytes:
    body = bytearray()
    iterator = getattr(response, "iter_content", None)
    if not callable(iterator):
        content = bytes(getattr(response, "content", b""))
        if len(content) > _MAX_RESPONSE_BYTES:
            raise ResearchTransportError("SearXNG response exceeded the size limit.")
        return content
    for chunk in iterator(chunk_size=64 * 1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ResearchTransportError("SearXNG response exceeded the size limit.")
    return bytes(body)
