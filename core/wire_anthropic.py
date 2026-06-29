"""Anthropic Messages API (/v1/messages) wire-format translation.

Translates the OpenAI-style messages/tools the agent loop produces into Anthropic's
shape, and translates Anthropic's streaming SSE events back into the same generic
events core/llm.py already yields ({text_delta, tool_call, done}).

Pure (stdlib only) so the translation — the bug-prone part — is unit-tested with
canned data, no network needed.

Run the self-check directly:  python3 core/wire_anthropic.py
"""

from __future__ import annotations

import json
from typing import Any

_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


def anthropic_endpoint(base_url: str) -> str:
    """Normalize a base URL to the Anthropic messages endpoint."""
    base = base_url.rstrip("/")
    if base.endswith("/messages"):
        return base
    if base.endswith("/v1"):
        return f"{base}/messages"
    return f"{base}/v1/messages"


def anthropic_headers(api_key: str) -> dict[str, str]:
    """Headers for Anthropic — never log the key."""
    return {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
    }


def translate_tools(openai_tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI function tools -> Anthropic tools (strip the `function` wrapper)."""
    if not openai_tools:
        return []
    out: list[dict[str, Any]] = []
    for t in openai_tools:
        fn = t.get("function", t)
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return out


def _loads_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def translate_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Return (system_text, anthropic_messages).

    - system messages are concatenated into the top-level system string.
    - assistant tool_calls become `tool_use` content blocks.
    - role:"tool" results become `tool_result` blocks inside a user turn;
      consecutive tool results are merged into ONE user message (Anthropic requires
      tool_results to share the user turn that answers the preceding assistant turn).
    """
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            flush_tool_results()
            content = msg.get("content")
            if content:
                system_parts.append(content if isinstance(content, str) else str(content))
        elif role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": str(msg.get("content", "")),
                }
            )
        elif role == "assistant":
            flush_tool_results()
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                blocks: list[dict[str, Any]] = []
                text = msg.get("content")
                if text:
                    blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": fn.get("name", ""),
                            "input": _loads_args(fn.get("arguments")),
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": msg.get("content", "")})
        else:  # user (or anything else) → user text turn
            flush_tool_results()
            out.append({"role": "user", "content": msg.get("content", "")})

    flush_tool_results()
    return "\n\n".join(system_parts), out


def to_anthropic_payload(
    messages: list[dict[str, Any]],
    model: str,
    tools: list[dict[str, Any]] | None,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    stream: bool = True,
) -> dict[str, Any]:
    """Build a complete Anthropic /v1/messages request body."""
    system_text, a_messages = translate_messages(messages)
    payload: dict[str, Any] = {
        "model": model,
        "messages": a_messages,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    if system_text:
        payload["system"] = system_text
    a_tools = translate_tools(tools)
    if a_tools:
        payload["tools"] = a_tools
    return payload


def parse_nonstream_text(body: dict[str, Any]) -> str:
    """Extract final text from a non-streaming Anthropic response body."""
    parts: list[str] = []
    for block in body.get("content", []) or []:
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


class AnthropicStreamTranslator:
    """Feed parsed Anthropic SSE event dicts; get generic events back.

    Emits {"type":"text_delta","content":...} and
    {"type":"tool_call","id","name","arguments"} (arguments is a JSON string, to
    match the OpenAI path). Does NOT emit "done" — the caller does that when the
    HTTP stream ends, mirroring core/llm.py's OpenAI path.
    """

    def __init__(self) -> None:
        self._tool_blocks: dict[int, dict[str, str]] = {}

    def feed(self, chunk: dict[str, Any]) -> list[dict[str, Any]]:
        etype = chunk.get("type")
        out: list[dict[str, Any]] = []

        if etype == "content_block_start":
            block = chunk.get("content_block", {}) or {}
            if block.get("type") == "tool_use":
                self._tool_blocks[chunk.get("index", 0)] = {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": "",
                }
        elif etype == "content_block_delta":
            delta = chunk.get("delta", {}) or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                text = delta.get("text", "")
                if text:
                    out.append({"type": "text_delta", "content": text})
            elif dtype == "input_json_delta":
                idx = chunk.get("index", 0)
                if idx in self._tool_blocks:
                    self._tool_blocks[idx]["arguments"] += delta.get("partial_json", "")
            # thinking_delta and others are intentionally dropped.
        elif etype == "content_block_stop":
            idx = chunk.get("index", 0)
            tb = self._tool_blocks.pop(idx, None)
            if tb is not None:
                out.append(
                    {
                        "type": "tool_call",
                        "id": tb["id"],
                        "name": tb["name"],
                        "arguments": tb["arguments"] or "{}",
                    }
                )
        # message_start / message_delta / message_stop need no generic event.
        return out


def _self_check() -> None:
    # endpoint + headers
    assert anthropic_endpoint("https://api.anthropic.com") == "https://api.anthropic.com/v1/messages"
    assert anthropic_endpoint("https://api.anthropic.com/v1") == "https://api.anthropic.com/v1/messages"
    assert anthropic_headers("k")["x-api-key"] == "k"
    assert "anthropic-version" in anthropic_headers("k")

    # tool translation
    oai_tools = [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}]
    at = translate_tools(oai_tools)
    assert at == [{"name": "f", "description": "d", "input_schema": {"type": "object"}}], at

    # message translation: system extracted, tool_use block, merged tool_results
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "tool_calls": [
            {"id": "t1", "function": {"name": "a", "arguments": '{"x":1}'}},
            {"id": "t2", "function": {"name": "b", "arguments": '{"y":2}'}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "r1"},
        {"role": "tool", "tool_call_id": "t2", "content": "r2"},
        {"role": "assistant", "content": "done"},
    ]
    system, conv = translate_messages(msgs)
    assert system == "sys"
    assert conv[0] == {"role": "user", "content": "hi"}
    assert conv[1]["role"] == "assistant" and conv[1]["content"][0]["type"] == "tool_use"
    assert conv[1]["content"][0]["input"] == {"x": 1}
    # two tool results merged into ONE user turn
    assert conv[2]["role"] == "user" and len(conv[2]["content"]) == 2
    assert conv[2]["content"][0]["tool_use_id"] == "t1"
    assert conv[3] == {"role": "assistant", "content": "done"}

    payload = to_anthropic_payload(msgs, "claude-x", oai_tools, max_tokens=100)
    assert payload["model"] == "claude-x" and payload["max_tokens"] == 100
    assert payload["system"] == "sys" and "tools" in payload

    # non-stream parse
    assert parse_nonstream_text({"content": [{"type": "text", "text": "A"}, {"type": "tool_use"}, {"type": "text", "text": "B"}]}) == "AB"

    # streaming translator with a canned Anthropic event sequence
    tr = AnthropicStreamTranslator()
    events: list[dict] = []
    seq = [
        {"type": "message_start"},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "tu1", "name": "search"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"q":'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '"cats"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_stop"},
    ]
    for ch in seq:
        events.extend(tr.feed(ch))
    texts = "".join(e["content"] for e in events if e["type"] == "text_delta")
    tool_calls = [e for e in events if e["type"] == "tool_call"]
    assert texts == "Hello", texts
    assert len(tool_calls) == 1 and tool_calls[0]["name"] == "search"
    assert tool_calls[0]["arguments"] == '{"q":"cats"}', tool_calls[0]["arguments"]

    print("wire_anthropic self-check: OK")


if __name__ == "__main__":
    _self_check()
