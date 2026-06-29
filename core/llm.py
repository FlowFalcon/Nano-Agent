"""
Multi-provider async LLM caller with fallback routing.

Provides:
* :func:`stream_llm_response` — streaming async generator that yields structured
  dicts for text deltas, tool calls, completion, and errors.
* :func:`call_llm_simple` — non-streaming helper for internal summarisation.

Reasoning-safety note:
Some OpenAI-compatible providers expose model reasoning in separate fields such
as ``reasoning_content``. This module deliberately ignores those fields and only
emits final-answer ``content`` to the UI layer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp

from config.settings import LLMProvider
from core.output_filter import sanitize_user_visible_text

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503})


def _build_headers(provider: LLMProvider) -> dict[str, str]:
    """Build request headers — never log the API key."""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {provider.api_key}",
    }


def _build_payload(
    messages: list[dict[str, Any]],
    provider: LLMProvider,
    tools: list[dict[str, Any]] | None,
    *,
    stream: bool = True,
) -> dict[str, Any]:
    """Build the OpenAI-compatible ``/chat/completions`` request body."""
    payload: dict[str, Any] = {
        "model": provider.model,
        "messages": messages,
        "stream": stream,
    }

    # Optional provider-specific parameters. Example for providers that support
    # it: {"thinking": {"type": "disabled"}}. Leave empty unless documented;
    # unknown fields can break some gateways.
    extra_body = getattr(provider, "extra_body", None)
    if extra_body:
        payload.update(extra_body)

    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    return payload


def _endpoint(provider: LLMProvider) -> str:
    """Normalise the provider base URL and append the completions path."""
    base = provider.base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _models_for(provider: LLMProvider) -> list[str]:
    """Ordered, de-duplicated models to try for a provider: primary first, then
    any extra fallback models (blueprint 5b: model-level fallback before provider)."""
    extra = getattr(provider, "models", None) or []
    ordered: list[str] = []
    for m in [provider.model, *extra]:
        if m and m not in ordered:
            ordered.append(m)
    return ordered


def _anthropic_max_tokens(provider: LLMProvider) -> int:
    """max_tokens is required by Anthropic; allow override via extra_body."""
    extra = getattr(provider, "extra_body", None) or {}
    mt = extra.get("max_tokens")
    return int(mt) if isinstance(mt, int) and mt > 0 else 4096


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Parse a single ``data: {...}`` SSE line into a dict."""
    line = line.strip()
    if not line or line.startswith(":"):
        return None
    if not line.startswith("data: "):
        return None

    data_str = line[6:]
    if data_str.strip() == "[DONE]":
        return None

    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        logger.debug("Failed to parse SSE chunk: %s", data_str[:200])
        return None


def _has_reasoning_delta(delta: dict[str, Any]) -> bool:
    """Return True if a provider sent reasoning-only fields."""
    return bool(
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or delta.get("thinking")
    )


async def stream_llm_response(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    providers: list[LLMProvider],
    timeout: float = 60.0,
) -> AsyncGenerator[dict[str, Any], None]:
    """Stream a chat completion across providers with fallback.

    Yields:
      * ``{"type": "text_delta", "content": "..."}``
      * ``{"type": "tool_call", "id": "...", "name": "...", "arguments": "..."}``
      * ``{"type": "done"}``
      * ``{"type": "error", "content": "..."}``
    """
    if not providers:
        yield {"type": "error", "content": "No LLM providers configured."}
        return

    last_error = ""

    # Two-level fallback: each (provider, model) is one attempt, models within a
    # provider tried before moving to the next provider.
    attempts = [(p, m) for p in providers for m in _models_for(p)]
    for provider, model in attempts:
        if getattr(provider, "wire_format", "openai") == "anthropic":
            from core.wire_anthropic import (
                AnthropicStreamTranslator,
                anthropic_endpoint,
                anthropic_headers,
                to_anthropic_payload,
            )
            a_url = anthropic_endpoint(provider.base_url)
            a_headers = anthropic_headers(provider.api_key)
            a_payload = to_anthropic_payload(messages, model, tools, max_tokens=_anthropic_max_tokens(provider), stream=True)
            try:
                client_timeout = aiohttp.ClientTimeout(total=timeout)
                async with aiohttp.ClientSession(timeout=client_timeout) as session:
                    async with session.post(a_url, json=a_payload, headers=a_headers) as resp:
                        if resp.status in _RETRYABLE_STATUS_CODES:
                            last_error = f"Provider '{provider.name}' (anthropic) HTTP {resp.status}"
                            logger.warning("LLM provider '%s' (anthropic) returned %d, falling back.", provider.name, resp.status)
                            continue
                        if resp.status != 200:
                            body = await resp.text()
                            yield {"type": "error", "content": f"Provider '{provider.name}' returned HTTP {resp.status}: {body[:500]}"}
                            return
                        translator = AnthropicStreamTranslator()
                        async for raw_line in resp.content:
                            line = raw_line.decode("utf-8", errors="replace")
                            for sub_line in line.split("\n"):
                                chunk = _parse_sse_line(sub_line)
                                if chunk is None:
                                    continue
                                for ev in translator.feed(chunk):
                                    yield ev
                        yield {"type": "done"}
                        return
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                last_error = f"Provider '{provider.name}' (anthropic): {type(exc).__name__}: {exc}"
                logger.warning("LLM provider '%s' (anthropic) failed: %s, falling back.", provider.name, exc)
                continue

        url = _endpoint(provider)
        headers = _build_headers(provider)
        payload = _build_payload(messages, provider, tools, stream=True)
        payload["model"] = model

        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status in _RETRYABLE_STATUS_CODES:
                        body_preview = (await resp.text())[:300]
                        last_error = (
                            f"Provider '{provider.name}' returned HTTP {resp.status}: "
                            f"{body_preview}"
                        )
                        logger.warning(
                            "LLM provider '%s' returned %d, falling back.",
                            provider.name,
                            resp.status,
                        )
                        continue

                    if resp.status != 200:
                        body = await resp.text()
                        yield {
                            "type": "error",
                            "content": (
                                f"Provider '{provider.name}' returned HTTP {resp.status}: "
                                f"{body[:500]}"
                            ),
                        }
                        return

                    tool_calls_acc: dict[int, dict[str, str]] = {}

                    async for raw_line in resp.content:
                        line = raw_line.decode("utf-8", errors="replace")
                        for sub_line in line.split("\n"):
                            chunk = _parse_sse_line(sub_line)
                            if chunk is None:
                                continue

                            choices = chunk.get("choices", [])
                            if not choices:
                                continue

                            choice = choices[0]
                            finish_reason = choice.get("finish_reason")
                            delta = choice.get("delta", {}) or {}

                            # Never forward provider-specific reasoning fields
                            # to the channel. Only final-answer content is emitted.
                            if _has_reasoning_delta(delta):
                                logger.debug(
                                    "Dropped streamed reasoning field from %s",
                                    provider.name,
                                )

                            text_content = delta.get("content")
                            if text_content:
                                yield {"type": "text_delta", "content": text_content}

                            tc_list = delta.get("tool_calls")
                            if tc_list:
                                for tc in tc_list:
                                    idx = tc.get("index", 0)
                                    if idx not in tool_calls_acc:
                                        tool_calls_acc[idx] = {
                                            "id": tc.get("id", ""),
                                            "name": "",
                                            "arguments": "",
                                        }

                                    if tc.get("id"):
                                        tool_calls_acc[idx]["id"] = tc["id"]

                                    fn = tc.get("function", {}) or {}
                                    if fn.get("name"):
                                        tool_calls_acc[idx]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        tool_calls_acc[idx]["arguments"] += fn["arguments"]

                            if finish_reason == "tool_calls" or (
                                finish_reason == "stop" and tool_calls_acc
                            ):
                                for idx in sorted(tool_calls_acc):
                                    tc_data = tool_calls_acc[idx]
                                    yield {
                                        "type": "tool_call",
                                        "id": tc_data["id"],
                                        "name": tc_data["name"],
                                        "arguments": tc_data["arguments"],
                                    }
                                tool_calls_acc.clear()

                    # Some providers finish the SSE stream without a final
                    # finish_reason chunk. Emit any accumulated tool calls.
                    for idx in sorted(tool_calls_acc):
                        tc_data = tool_calls_acc[idx]
                        yield {
                            "type": "tool_call",
                            "id": tc_data["id"],
                            "name": tc_data["name"],
                            "arguments": tc_data["arguments"],
                        }

                    yield {"type": "done"}
                    return

        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            last_error = f"Provider '{provider.name}': {type(exc).__name__}: {exc}"
            logger.warning("LLM provider '%s' failed: %s, falling back.", provider.name, exc)
            continue

    yield {"type": "error", "content": f"All LLM providers failed. Last error: {last_error}"}


async def call_llm_simple(
    messages: list[dict[str, Any]],
    providers: list[LLMProvider],
    timeout: float = 30.0,
) -> str:
    """Non-streaming LLM call for internal use, returning final content only."""
    if not providers:
        raise RuntimeError("No LLM providers configured for call_llm_simple.")

    last_error = ""

    attempts = [(p, m) for p in providers for m in _models_for(p)]
    for provider, model in attempts:
        if getattr(provider, "wire_format", "openai") == "anthropic":
            from core.wire_anthropic import anthropic_endpoint, anthropic_headers, parse_nonstream_text, to_anthropic_payload
            a_url = anthropic_endpoint(provider.base_url)
            a_headers = anthropic_headers(provider.api_key)
            a_payload = to_anthropic_payload(messages, model, None, max_tokens=_anthropic_max_tokens(provider), stream=False)
            try:
                client_timeout = aiohttp.ClientTimeout(total=timeout)
                async with aiohttp.ClientSession(timeout=client_timeout) as session:
                    async with session.post(a_url, json=a_payload, headers=a_headers) as resp:
                        if resp.status in _RETRYABLE_STATUS_CODES:
                            last_error = f"Provider '{provider.name}' (anthropic) HTTP {resp.status}"
                            continue
                        body = await resp.json()
                        if resp.status != 200:
                            raise RuntimeError(f"Provider '{provider.name}' (anthropic) HTTP {resp.status}: {body}")
                        return sanitize_user_visible_text(parse_nonstream_text(body))
            except (aiohttp.ClientError, TimeoutError, OSError) as exc:
                last_error = f"Provider '{provider.name}' (anthropic): {type(exc).__name__}: {exc}"
                continue

        url = _endpoint(provider)
        headers = _build_headers(provider)
        payload = _build_payload(messages, provider, tools=None, stream=False)
        payload["model"] = model

        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    try:
                        body = await resp.json()
                    except Exception:
                        body_text = await resp.text()
                        body = {"error": {"message": body_text}}

                    if resp.status in _RETRYABLE_STATUS_CODES:
                        last_error = f"Provider '{provider.name}' returned HTTP {resp.status}"
                        logger.warning(
                            "call_llm_simple: provider '%s' returned %d, falling back.",
                            provider.name,
                            resp.status,
                        )
                        continue

                    if resp.status != 200:
                        error_msg = body.get("error", {}).get("message", str(body))
                        raise RuntimeError(
                            f"Provider '{provider.name}' HTTP {resp.status}: {error_msg}"
                        )

                    choices = body.get("choices", [])
                    if not choices:
                        raise RuntimeError(
                            f"Provider '{provider.name}' returned no choices."
                        )

                    message = choices[0].get("message", {}) or {}
                    if message.get("reasoning_content") or message.get("reasoning"):
                        logger.debug(
                            "Dropped non-streaming reasoning field from %s",
                            provider.name,
                        )

                    return sanitize_user_visible_text(message.get("content", "") or "")

        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            last_error = f"Provider '{provider.name}': {type(exc).__name__}: {exc}"
            logger.warning(
                "call_llm_simple: provider '%s' failed: %s, falling back.",
                provider.name,
                exc,
            )
            continue

    raise RuntimeError(f"All LLM providers failed in call_llm_simple. Last: {last_error}")
