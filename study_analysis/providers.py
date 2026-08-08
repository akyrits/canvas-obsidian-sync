from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import requests

from .schema import analysis_json_schema


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float | None = None
    thinking_tokens: int | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    model: str | None = None
    stop_reason: str | None = None
    request_id: str | None = None
    service_tier: str | None = None
    inference_geo: str | None = None

    @property
    def total_input_tokens(self) -> int:
        """Logical input across uncached, cache-write, and cache-read counters."""
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )

    @property
    def non_thinking_output_tokens(self) -> int | None:
        """Approximate visible/non-reasoning output from provider usage details."""
        if self.thinking_tokens is None:
            return None
        return max(0, self.output_tokens - self.thinking_tokens)


@dataclass(frozen=True)
class ModelReply:
    payload: dict[str, Any]
    usage: ModelUsage = ModelUsage()


class ModelInvocationError(RuntimeError):
    """A failed model attempt whose provider usage is still safe to record."""

    def __init__(
        self,
        message: str,
        usage: ModelUsage | None = None,
        kind: str = "model_error",
    ):
        super().__init__(message)
        self.usage = usage
        self.kind = kind


class ModelAdapter(Protocol):
    """The only model seam the analysis module exposes to adapters."""

    name: str

    def generate_json(self, prompt: str, max_output_tokens: int) -> ModelReply: ...


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("model response must be a JSON object")
    return payload


class StaticJsonAdapter:
    """Deterministic adapter for tests, pilots, and resumable saved responses."""

    name = "static-json"
    uses_transport_schema = True

    def __init__(self, response_path: Path):
        self.response_path = response_path

    def generate_json(self, prompt: str, max_output_tokens: int) -> ModelReply:
        del prompt, max_output_tokens
        return ModelReply(_parse_json(self.response_path.read_text(encoding="utf-8")))


class AnthropicAdapter:
    name = "anthropic"
    uses_transport_schema = True

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        effort: str = "medium",
        thinking: str = "adaptive",
    ):
        if effort not in {"low", "medium", "high", "max"}:
            raise ValueError("Anthropic effort must be low, medium, high, or max")
        if thinking not in {"adaptive", "disabled"}:
            raise ValueError("Anthropic thinking must be adaptive or disabled")
        self.model = model
        self.api_key = api_key
        self.effort = effort
        self.thinking = thinking

    def generate_json(self, prompt: str, max_output_tokens: int) -> ModelReply:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=max_output_tokens,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": self.thinking},
                output_config={
                    "effort": self.effort,
                    "format": {
                        "type": "json_schema",
                        "schema": analysis_json_schema(),
                    },
                },
            )
        except Exception as exc:
            raise ModelInvocationError(
                "Anthropic request failed before usage was returned.",
                kind="request_error",
            ) from exc
        text = "\n".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        output_details = getattr(usage, "output_tokens_details", None)
        cache_creation = getattr(usage, "cache_creation", None)
        model_usage = ModelUsage(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            thinking_tokens=(
                getattr(output_details, "thinking_tokens", None)
                if output_details is not None
                else None
            ),
            cache_creation_input_tokens=(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            ),
            cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_5m_input_tokens=(
                getattr(cache_creation, "ephemeral_5m_input_tokens", 0) or 0
            ),
            cache_creation_1h_input_tokens=(
                getattr(cache_creation, "ephemeral_1h_input_tokens", 0) or 0
            ),
            model=str(getattr(response, "model", self.model) or self.model),
            stop_reason=str(getattr(response, "stop_reason", "") or "") or None,
            request_id=str(getattr(response, "_request_id", "") or "") or None,
            service_tier=str(getattr(usage, "service_tier", "") or "") or None,
            inference_geo=str(getattr(usage, "inference_geo", "") or "") or None,
        )
        if response.stop_reason == "max_tokens":
            raise ModelInvocationError(
                "Anthropic response reached the output token limit before completing; "
                f"increase max_output_tokens above {max_output_tokens}. "
                f"This attempt used {model_usage.total_input_tokens:,} input and "
                f"{model_usage.output_tokens:,} output tokens.",
                model_usage,
                "max_tokens",
            )
        if response.stop_reason == "refusal":
            raise ModelInvocationError(
                "Anthropic refused the structured analysis request.",
                model_usage,
                "refusal",
            )
        if response.stop_reason != "end_turn":
            raise ModelInvocationError(
                f"Anthropic response stopped before completion: {response.stop_reason}",
                model_usage,
                "incomplete",
            )
        try:
            payload = _parse_json(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelInvocationError(
                "Anthropic returned invalid structured JSON.",
                model_usage,
                "invalid_json",
            ) from exc
        return ModelReply(
            payload,
            model_usage,
        )


class OpenAICompatibleAdapter:
    """Works with OpenAI-compatible hosted or local endpoints (Ollama/LM Studio)."""

    name = "openai-compatible"
    uses_transport_schema = False

    def __init__(self, base_url: str, model: str, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key

    def generate_json(self, prompt: str, max_output_tokens: int) -> ModelReply:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=headers,
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_output_tokens,
                "temperature": 0.1,
            },
            timeout=180,
        )
        response.raise_for_status()
        body = response.json()
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        finish_reason = body["choices"][0].get("finish_reason")
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        cached_tokens = int(prompt_details.get("cached_tokens") or 0)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        model_usage = ModelUsage(
            input_tokens=max(0, prompt_tokens - cached_tokens),
            output_tokens=int(usage.get("completion_tokens") or 0),
            thinking_tokens=(
                int(completion_details["reasoning_tokens"])
                if completion_details.get("reasoning_tokens") is not None
                else None
            ),
            cache_read_input_tokens=cached_tokens,
            model=str(body.get("model") or self.model),
            stop_reason=str(finish_reason) if finish_reason else None,
            request_id=(body.get("id") or response.headers.get("x-request-id")),
        )
        if finish_reason == "length":
            raise ModelInvocationError(
                "OpenAI-compatible response reached the output token limit before completing.",
                model_usage,
                "max_tokens",
            )
        try:
            payload = _parse_json(text)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelInvocationError(
                "OpenAI-compatible endpoint returned invalid JSON.",
                model_usage,
                "invalid_json",
            ) from exc
        return ModelReply(
            payload,
            model_usage,
        )


def adapter_from_env(response_file: Path | None = None) -> ModelAdapter:
    if response_file is not None:
        return StaticJsonAdapter(response_file)

    provider = os.environ.get("MODEL_PROVIDER", "").strip().lower()
    model = os.environ.get("MODEL_NAME", "").strip()
    if not provider or not model:
        raise RuntimeError(
            "Set MODEL_PROVIDER and MODEL_NAME, or pass --response-file for a saved response."
        )
    if provider == "anthropic":
        return AnthropicAdapter(
            model,
            os.environ.get("ANTHROPIC_API_KEY"),
            effort=os.environ.get("MODEL_EFFORT", "medium").strip().lower(),
            thinking=os.environ.get("MODEL_THINKING", "adaptive").strip().lower(),
        )
    if provider in {"openai", "openai-compatible", "ollama", "lm-studio"}:
        base_url = os.environ.get("MODEL_BASE_URL", "").strip()
        if not base_url:
            raise RuntimeError("MODEL_BASE_URL is required for an OpenAI-compatible provider")
        return OpenAICompatibleAdapter(
            base_url, model, os.environ.get("MODEL_API_KEY")
        )
    raise RuntimeError(f"Unsupported MODEL_PROVIDER: {provider}")
