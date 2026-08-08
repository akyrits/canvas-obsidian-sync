from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from study_analysis.research import (
    AdapterReply,
    ResearchCandidate,
    ResearchEngine,
    ResearchRequest,
    ResearchTransportError,
    ResearchValidationError,
    SavedJsonResearchAdapter,
    SearXNGHttpAdapter,
    build_public_concept_query,
    research_adapter_from_env,
    validate_research_cache_path,
)


def saved_payload(
    request: ResearchRequest, results: list[dict[str, str]]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "request_sha256": request.sha256,
        "retrieved_at": "2026-07-01T12:00:00+00:00",
        "results": results,
    }


class FakeAdapter:
    name = "fake-search"
    source = "live"
    cacheable = True
    cache_namespace = "fake-search:test"

    def __init__(
        self,
        candidates: tuple[ResearchCandidate, ...] = (),
        *,
        cost: float | None = None,
        failure: Exception | None = None,
    ):
        self.candidates = candidates
        self.cost = cost
        self.failure = failure
        self.calls = 0

    def fetch(self, request: ResearchRequest) -> AdapterReply:
        del request
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return AdapterReply(self.candidates, provider_requests=1, estimated_cost_usd=self.cost)


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.status_code = status_code
        self.body = json.dumps(payload).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index : index + chunk_size]

    def close(self) -> None:
        self.closed = True


class ResearchEngineTests(unittest.TestCase):
    def test_public_concept_query_is_bounded_and_rejects_contact_or_url_data(self) -> None:
        query = build_public_concept_query(
            ("Binary Trees", "Tree Depth, Height, and Levels")
        )

        self.assertIn('"Binary Trees"', query)
        self.assertIn('"Tree Depth Height and Levels"', query)
        self.assertLessEqual(len(query), 512)
        for unsafe in (
            ("https://private.example/path",),
            ("student@example.edu",),
            ("x" * 81,),
            ("Binary Trees", "binary trees"),
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(
                ResearchValidationError
            ):
                build_public_concept_query(unsafe)

    def test_saved_json_replay_is_deterministic_token_free_and_non_cacheable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            response_path = root / "response.json"
            request = ResearchRequest("binary tree traversal")
            response_path.write_text(
                json.dumps(
                    saved_payload(
                        request,
                        [
                            {
                                "title": "Tree traversal reference",
                                "url": "https://example.edu/trees/traversal",
                                "snippet": "A synthetic normalized test result.",
                            }
                        ],
                    )
                ),
                encoding="utf-8",
            )
            cache = root / "cache"
            engine = ResearchEngine(SavedJsonResearchAdapter(response_path), cache_dir=cache)

            first = engine.search(request)
            second = engine.search(request)

            self.assertEqual(first, second)
            self.assertEqual(first.usage.source, "replay")
            self.assertEqual(first.usage.provider_requests, 0)
            self.assertEqual(first.usage.estimated_cost_usd, 0.0)
            self.assertFalse(first.usage.model_attempted)
            self.assertEqual(first.usage.input_tokens, 0)
            self.assertEqual(first.usage.output_tokens, 0)
            self.assertFalse(cache.exists())

    def test_assignment6_replay_is_bound_to_reviewed_topics_and_has_no_snippets(self) -> None:
        request = ResearchRequest(
            build_public_concept_query(
                (
                    "General Trees",
                    "Tree Depth, Height, and Levels",
                    "Proper Binary Trees",
                    "Tree Traversal",
                    "Arithmetic Expression Trees",
                    "Tree Reconstruction from Traversals",
                )
            ),
            max_results=5,
        )
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "assignment6_research_results.json"
        )

        outcome = ResearchEngine(SavedJsonResearchAdapter(fixture)).search(request)

        self.assertEqual(len(outcome.hits), 5)
        self.assertTrue(all(not hit.snippet for hit in outcome.hits))
        self.assertEqual(outcome.usage.source, "replay")
        self.assertEqual(outcome.usage.provider_requests, 0)
        self.assertEqual(outcome.usage.estimated_cost_usd, 0.0)

    def test_request_validation_happens_before_transport(self) -> None:
        for query, limit in [("", 5), ("   ", 5), ("x" * 513, 5), ("valid", 0), ("valid", 11)]:
            with self.subTest(query_length=len(query), limit=limit):
                with self.assertRaises(ResearchValidationError):
                    ResearchRequest(query, limit)

    def test_results_are_normalized_ranked_deduplicated_and_capped(self) -> None:
        adapter = FakeAdapter(
            (
                ResearchCandidate(
                    "<b>First</b>\x00 result",
                    "https://EXAMPLE.com:443/path?utm_source=test&a=1#section",
                    "  useful   summary  ",
                ),
                ResearchCandidate("Duplicate", "https://example.com/path?a=1"),
                ResearchCandidate("Semantic query", "https://example.com/path?a=2"),
                ResearchCandidate("Over cap", "https://example.org/other"),
            )
        )
        outcome = ResearchEngine(adapter).search(
            ResearchRequest("trees", max_results=2)
        )

        self.assertEqual(
            [hit.url for hit in outcome.hits],
            ["https://example.com/path?a=1", "https://example.com/path?a=2"],
        )
        self.assertEqual(outcome.hits[0].title, "First result")
        self.assertEqual(outcome.hits[0].snippet, "useful summary")
        self.assertEqual(outcome.dropped_hits, 2)

    def test_unsafe_and_malformed_hits_are_dropped(self) -> None:
        adapter = FakeAdapter(
            (
                ResearchCandidate("Script", "javascript:alert(1)"),
                ResearchCandidate("Plain HTTP", "http://example.com/page"),
                ResearchCandidate("Credentials", "https://user:pass@example.com/page"),
                ResearchCandidate("Loopback", "https://localhost/page"),
                ResearchCandidate("Private IP", "https://10.0.0.1/page"),
                ResearchCandidate("Bad host", "https://exa mple.com/page"),
                ResearchCandidate("", "https://example.com/no-title"),
                ResearchCandidate(
                    "Encoded &lt;img src=x onerror=alert(1)&gt; Valid",
                    "https://example.com/good",
                ),
            )
        )
        outcome = ResearchEngine(adapter).search(ResearchRequest("safe research"))

        self.assertEqual([hit.title for hit in outcome.hits], ["Encoded Valid"])
        self.assertNotIn("<img", outcome.hits[0].title)
        self.assertEqual(outcome.dropped_hits, 7)

    def test_markdown_angle_destination_escape_urls_are_dropped(self) -> None:
        adapter = FakeAdapter(
            (
                ResearchCandidate(
                    "Closing delimiter escape",
                    "https://example.com/a>)[Injected](<https://evil.example/",
                ),
                ResearchCandidate(
                    "Opening delimiter",
                    "https://example.com/a<provider-controlled",
                ),
                ResearchCandidate(
                    "Safely encoded delimiter",
                    "https://example.com/a%3Eb",
                ),
            )
        )

        outcome = ResearchEngine(adapter).search(
            ResearchRequest("markdown destination safety")
        )

        self.assertEqual(
            [hit.url for hit in outcome.hits],
            ["https://example.com/a%3Eb"],
        )
        self.assertEqual(outcome.dropped_hits, 2)
        self.assertNotIn("<", outcome.hits[0].url)
        self.assertNotIn(">", outcome.hits[0].url)

    def test_cache_hit_skips_transport_and_omits_raw_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "cache"
            candidate = ResearchCandidate(
                "Cached result", "https://example.com/cache", "Public snippet"
            )
            first_adapter = FakeAdapter((candidate,), cost=0.125)
            request = ResearchRequest("PRIVATE-QUERY-SENTINEL")
            first = ResearchEngine(first_adapter, cache_dir=cache).search(request)
            self.assertEqual(first_adapter.calls, 1)

            second_adapter = FakeAdapter(failure=AssertionError("transport must not run"))
            second = ResearchEngine(second_adapter, cache_dir=cache).search(request)

            self.assertEqual(second.hits, first.hits)
            self.assertEqual(second.usage.source, "cache")
            self.assertEqual(second.usage.provider_requests, 0)
            self.assertEqual(second.usage.estimated_cost_usd, 0.0)
            self.assertEqual(second_adapter.calls, 0)
            cache_text = next(cache.glob("*.json")).read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE-QUERY-SENTINEL", cache_text)

    def test_expired_corrupt_and_wrong_version_cache_entries_refetch(self) -> None:
        scenarios = ("expired", "corrupt", "wrong-version", "unsafe-hit")
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                cache = Path(temp) / "cache"
                start = datetime(2026, 7, 1, tzinfo=timezone.utc)
                request = ResearchRequest("cache recovery")
                seed = FakeAdapter(
                    (ResearchCandidate("First", "https://example.com/first"),)
                )
                first = ResearchEngine(
                    seed, cache_dir=cache, clock=lambda: start
                ).search(request)
                cache_path = next(cache.glob("*.json"))
                clock = lambda: start
                if scenario == "expired":
                    clock = lambda: start + timedelta(days=8)
                elif scenario == "corrupt":
                    cache_path.write_text("not-json", encoding="utf-8")
                else:
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    if scenario == "wrong-version":
                        payload["schema_version"] = 999
                    else:
                        payload["hits"][0]["url"] = "javascript:alert(1)"
                    cache_path.write_text(json.dumps(payload), encoding="utf-8")

                replacement = FakeAdapter(
                    (ResearchCandidate("Second", "https://example.com/second"),)
                )
                outcome = ResearchEngine(
                    replacement, cache_dir=cache, clock=clock
                ).search(request)

                self.assertEqual(replacement.calls, 1)
                self.assertEqual(outcome.hits[0].title, "Second")
                self.assertNotEqual(outcome.hits, first.hits)

    def test_unknown_live_cost_remains_unknown(self) -> None:
        outcome = ResearchEngine(
            FakeAdapter((ResearchCandidate("One", "https://example.com/one"),))
        ).search(ResearchRequest("cost telemetry"))
        self.assertIsNone(outcome.usage.estimated_cost_usd)

    def test_searxng_maps_bounded_json_and_uses_safe_transport_options(self) -> None:
        captured: dict[str, object] = {}
        response = FakeResponse(
            {
                "results": [
                    {
                        "title": "Official trees guide",
                        "url": "https://example.edu/trees",
                        "content": "A concise guide.",
                    }
                ]
            }
        )

        def fake_get(url: str, **kwargs: object) -> FakeResponse:
            captured["url"] = url
            captured.update(kwargs)
            return response

        adapter = SearXNGHttpAdapter(
            "https://search.example.org/searxng", http_get=fake_get
        )
        outcome = ResearchEngine(adapter).search(ResearchRequest("binary trees", 3))

        self.assertEqual(captured["url"], "https://search.example.org/searxng/search")
        self.assertEqual(captured["params"], {"q": "binary trees", "format": "json", "safesearch": 1})
        self.assertEqual(captured["timeout"], (5.0, 20.0))
        self.assertFalse(captured["allow_redirects"])
        self.assertTrue(captured["stream"])
        self.assertEqual(outcome.hits[0].snippet, "A concise guide.")
        self.assertEqual(outcome.usage.provider_requests, 1)
        self.assertTrue(response.closed)

    def test_searxng_failures_are_redacted_and_insecure_endpoints_are_rejected(self) -> None:
        sentinel = "PRIVATE-QUERY-SENTINEL"

        def failing_get(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError(sentinel)

        adapter = SearXNGHttpAdapter(
            "http://127.0.0.1:8080", http_get=failing_get
        )
        with self.assertRaises(ResearchTransportError) as raised:
            adapter.fetch(ResearchRequest(sentinel))
        self.assertNotIn(sentinel, str(raised.exception))

        class StreamingFailure(FakeResponse):
            def iter_content(self, chunk_size: int):
                del chunk_size
                raise RuntimeError(sentinel)
                yield b""

        stream_adapter = SearXNGHttpAdapter(
            "https://search.example.org",
            http_get=lambda *args, **kwargs: StreamingFailure({}),
        )
        with self.assertRaises(ResearchTransportError) as stream_raised:
            stream_adapter.fetch(ResearchRequest(sentinel))
        self.assertNotIn(sentinel, str(stream_raised.exception))

        for endpoint in (
            "http://search.example.org",
            "https://user:password@search.example.org",
            "https://search.example.org/?token=secret",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ResearchValidationError):
                    SearXNGHttpAdapter(endpoint)

    def test_legacy_loopback_hosts_are_rejected_and_global_ipv6_is_bracketed(self) -> None:
        candidates = tuple(
            ResearchCandidate("Unsafe", f"https://{host}/page")
            for host in (
                "127.1",
                "2130706433",
                "0x7f000001",
                "127。0。0。1",
                "０x７f０００００１",
            )
        ) + (
            ResearchCandidate(
                "IPv6", "https://[2606:4700:4700::1111]:443/reference"
            ),
        )
        outcome = ResearchEngine(FakeAdapter(candidates)).search(
            ResearchRequest("numeric host validation")
        )
        self.assertEqual(outcome.dropped_hits, 5)
        self.assertEqual(
            outcome.hits[0].url,
            "https://[2606:4700:4700::1111]/reference",
        )

    def test_saved_replay_is_query_bound_and_size_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            intended = ResearchRequest("intended query")
            response_path = root / "response.json"
            response_path.write_text(
                json.dumps(saved_payload(intended, [])), encoding="utf-8"
            )
            adapter = SavedJsonResearchAdapter(response_path)
            with self.assertRaisesRegex(ResearchValidationError, "does not match"):
                adapter.fetch(ResearchRequest("unrelated query"))

            oversized = root / "oversized.json"
            oversized.write_bytes(b" " * 128_001)
            with self.assertRaisesRegex(ResearchValidationError, "size limit"):
                SavedJsonResearchAdapter(oversized)

    def test_cache_path_must_be_absolute_and_outside_protected_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            vault = root / "vault"
            repository = root / "repository"
            allowed = root.parent / "research-cache-test"
            with self.assertRaisesRegex(ResearchValidationError, "must be absolute"):
                validate_research_cache_path(
                    Path("relative-cache"), protected_roots=(repository, vault)
                )
            for unsafe in (repository / "cache", vault / "cache"):
                with self.assertRaisesRegex(ResearchValidationError, "outside"):
                    validate_research_cache_path(
                        unsafe, protected_roots=(repository, vault)
                    )
            self.assertEqual(
                validate_research_cache_path(
                    allowed, protected_roots=(repository, vault)
                ),
                allowed.resolve(strict=False),
            )

    def test_factory_is_opt_in_and_accepts_saved_replay_without_live_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            response_path = Path(temp) / "response.json"
            request = ResearchRequest("factory replay")
            response_path.write_text(
                json.dumps(saved_payload(request, [])), encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                adapter = research_adapter_from_env(response_path)
                self.assertIsInstance(adapter, SavedJsonResearchAdapter)
                with self.assertRaisesRegex(RuntimeError, "Live research is opt-in"):
                    research_adapter_from_env()


if __name__ == "__main__":
    unittest.main()
