"""Multi-provider web search with a fallback chain.

Order: DuckDuckGo (no key, via ``ddgs``) -> Brave -> Tavily. Brave and Tavily are
tried only when their API key is present (env ``BRAVE_API_KEY`` / ``TAVILY_API_KEY``).
If one provider errors or returns nothing, the next is tried.

Pure helpers (provider selection, result formatting) are stdlib-only and
self-tested; the network calls obviously are not.

Run the self-check directly:  python3 core/search.py
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_TAVILY_URL = "https://api.tavily.com/search"


def available_providers(env: dict | None = None) -> list[str]:
    """Ordered search providers usable right now. ddg is always available."""
    env = os.environ if env is None else env
    providers = ["ddg"]
    if env.get("BRAVE_API_KEY"):
        providers.append("brave")
    if env.get("TAVILY_API_KEY"):
        providers.append("tavily")
    return providers


def format_results(results: list[dict], query: str) -> str:
    """Render a list of {title,url,body} dicts to the agent-facing string."""
    if not results:
        return f"No results found for: {query}"
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title") or "No title"
        url = r.get("url") or ""
        body = r.get("body") or ""
        lines.append(f"{i}. **{title}**\n   URL: {url}\n   {body}")
    return "\n\n".join(lines)


def _ddg_sync(query: str, max_results: int) -> list[dict]:
    from ddgs import DDGS  # type: ignore[import-untyped]

    # ddgs aggregates several free backends; retry once since DuckDuckGo rate-limits
    # datacenter/VPS IPs aggressively.
    last_exc: Exception | None = None
    for _ in range(2):
        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, region="wt-wt", safesearch="off", max_results=max_results))
            return [
                {"title": r.get("title", ""), "url": r.get("href", r.get("link", "")), "body": r.get("body", r.get("snippet", ""))}
                for r in raw
            ]
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if last_exc is not None:
        raise last_exc
    return []


async def _brave(query: str, max_results: int, api_key: str) -> list[dict]:
    import aiohttp

    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": query, "count": str(max_results)}
    async with aiohttp.ClientSession() as session:
        async with session.get(_BRAVE_URL, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json()
    web = (data.get("web") or {}).get("results") or []
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "body": r.get("description", "")} for r in web[:max_results]]


async def _tavily(query: str, max_results: int, api_key: str) -> list[dict]:
    import aiohttp

    payload = {"api_key": api_key, "query": query, "max_results": max_results}
    async with aiohttp.ClientSession() as session:
        async with session.post(_TAVILY_URL, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            data = await resp.json()
    results = data.get("results") or []
    return [{"title": r.get("title", ""), "url": r.get("url", ""), "body": r.get("content", "")} for r in results[:max_results]]


async def _search_one(provider: str, query: str, max_results: int, env: dict) -> list[dict]:
    if provider == "ddg":
        return await asyncio.to_thread(_ddg_sync, query, max_results)
    if provider == "brave":
        return await _brave(query, max_results, env["BRAVE_API_KEY"])
    if provider == "tavily":
        return await _tavily(query, max_results, env["TAVILY_API_KEY"])
    return []


async def search(query: str, max_results: int = 5, env: dict | None = None) -> str:
    """Run the fallback chain and return a formatted result string (or an
    actionable message the agent can relay to the user)."""
    env = os.environ if env is None else env
    providers = available_providers(env)
    errors: list[str] = []
    for provider in providers:
        try:
            results = await _search_one(provider, query, max_results, env)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Search provider '%s' failed: %s, trying next.", provider, exc)
            errors.append(f"{provider}: {type(exc).__name__}")
            continue
        if results:
            return format_results(results, query)

    # Everything failed or returned nothing.
    if errors and providers == ["ddg"]:
        return (
            "Web search is temporarily unavailable — the free DuckDuckGo backend is likely "
            "rate-limiting this server's IP. For reliable search, set BRAVE_API_KEY or "
            "TAVILY_API_KEY in the environment (then Brave/Tavily are used as fallbacks). "
            f"(detail: {'; '.join(errors)[:160]})"
        )
    if errors:
        return f"Web search failed across all providers ({'; '.join(errors)[:200]})."
    return f"No results found for: {query}"


def _self_check() -> None:
    assert available_providers({}) == ["ddg"]
    assert available_providers({"BRAVE_API_KEY": "x"}) == ["ddg", "brave"]
    assert available_providers({"BRAVE_API_KEY": "x", "TAVILY_API_KEY": "y"}) == ["ddg", "brave", "tavily"]

    assert format_results([], "cats") == "No results found for: cats"
    out = format_results([{"title": "T", "url": "u", "body": "b"}], "q")
    assert "1. **T**" in out and "URL: u" in out and "b" in out

    print("search self-check: OK")


if __name__ == "__main__":
    _self_check()
