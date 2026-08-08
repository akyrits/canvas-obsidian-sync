from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from typing import Any, Iterator
from unittest import mock

from study_analysis.providers import OpenAICompatibleAdapter, adapter_from_env


def _ok_response(text: str = '{"ok": true}') -> mock.Mock:
    response = mock.Mock()
    response.raise_for_status.return_value = None
    response.headers = {}
    response.json.return_value = {
        "id": "msg_test",
        "model": "test-model",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    return response


@contextmanager
def _captured_post() -> Iterator[dict[str, Any]]:
    """Run a request and yield the JSON body the adapter actually sent."""
    captured: dict[str, Any] = {}

    def fake_post(url: str, **kwargs: Any) -> mock.Mock:
        captured["url"] = url
        captured["json"] = kwargs["json"]
        captured["headers"] = kwargs["headers"]
        return _ok_response()

    with mock.patch("study_analysis.providers.requests.post", side_effect=fake_post):
        yield captured


class OpenAICompatibleRequestBodyTests(unittest.TestCase):
    """The request body is the adapter's contract with every provider.

    Newer Claude models reject any sampling parameter outright — the endpoint
    answers `temperature` is deprecated for this model with HTTP 400 — so a
    hardcoded temperature silently makes whole model generations unreachable.
    Saved-response tests never build a live body, so only these catch it.
    """

    def test_temperature_is_omitted_by_default(self) -> None:
        adapter = OpenAICompatibleAdapter("https://example.test/v1", "some-model")
        with _captured_post() as captured:
            adapter.generate_json("prompt", max_output_tokens=64)
        self.assertNotIn("temperature", captured["json"])
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")
        self.assertEqual(captured["json"]["max_tokens"], 64)
        self.assertEqual(captured["json"]["model"], "some-model")

    def test_temperature_is_sent_when_explicitly_configured(self) -> None:
        adapter = OpenAICompatibleAdapter(
            "https://example.test/v1", "older-model", temperature=0.1
        )
        with _captured_post() as captured:
            adapter.generate_json("prompt", max_output_tokens=64)
        self.assertEqual(captured["json"]["temperature"], 0.1)

    def test_trailing_slash_on_base_url_does_not_double(self) -> None:
        adapter = OpenAICompatibleAdapter("https://example.test/v1/", "some-model")
        with _captured_post() as captured:
            adapter.generate_json("prompt", max_output_tokens=64)
        self.assertEqual(captured["url"], "https://example.test/v1/chat/completions")

    def test_api_key_becomes_a_bearer_header_and_is_omitted_when_absent(self) -> None:
        with _captured_post() as captured:
            OpenAICompatibleAdapter("https://example.test/v1", "m", "secret").generate_json(
                "prompt", max_output_tokens=8
            )
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")

        with _captured_post() as captured:
            OpenAICompatibleAdapter("https://example.test/v1", "m").generate_json(
                "prompt", max_output_tokens=8
            )
        self.assertNotIn("Authorization", captured["headers"])


class AdapterFromEnvTemperatureTests(unittest.TestCase):
    ENV = {
        "MODEL_PROVIDER": "openai-compatible",
        "MODEL_NAME": "some-model",
        "MODEL_BASE_URL": "https://example.test/v1",
    }

    def test_temperature_is_none_when_unset(self) -> None:
        with mock.patch.dict(os.environ, self.ENV, clear=True):
            adapter = adapter_from_env()
        self.assertIsNone(adapter.temperature)

    def test_temperature_is_parsed_from_the_environment(self) -> None:
        with mock.patch.dict(os.environ, {**self.ENV, "MODEL_TEMPERATURE": "0.4"}, clear=True):
            adapter = adapter_from_env()
        self.assertEqual(adapter.temperature, 0.4)

    def test_blank_temperature_is_treated_as_unset(self) -> None:
        with mock.patch.dict(os.environ, {**self.ENV, "MODEL_TEMPERATURE": "  "}, clear=True):
            adapter = adapter_from_env()
        self.assertIsNone(adapter.temperature)

    def test_non_numeric_temperature_is_rejected_before_any_request(self) -> None:
        with mock.patch.dict(os.environ, {**self.ENV, "MODEL_TEMPERATURE": "warm"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MODEL_TEMPERATURE must be a number"):
                adapter_from_env()


if __name__ == "__main__":
    unittest.main()
