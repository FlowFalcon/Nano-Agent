"""Fill blank secrets in a raw config dict from environment variables.

Lets ``config.json`` ship WITHOUT secrets (headless / Docker / panel deploys):
leave ``bot_token`` and provider ``api_key`` empty in the file and supply them
via env instead. Pure stdlib so it can be unit-tested without pydantic.

Honored variables:
- ``TELEGRAM_BOT_TOKEN``                  → telegram.bot_token (if blank)
- ``LLM_API_KEY_<PROVIDER_NAME>``         → that provider's api_key (if blank)
- ``LLM_API_KEY``                         → fallback api_key for any blank provider

Provider name match is upper-cased with ``-``/spaces → ``_`` (e.g. provider
"open-router" → ``LLM_API_KEY_OPEN_ROUTER``). Values are never logged.

Run the self-check directly:  ``python3 config/env_overrides.py``
"""

from __future__ import annotations

import copy
import os


def _norm(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.strip().upper())


def apply_env_overrides(raw: dict, env: dict | None = None) -> dict:
    """Return a NEW config dict with blank secrets filled from *env* (os.environ).

    Only fills values that are missing or empty — an explicit value in the file
    always wins. Never mutates the input dict.
    """
    if env is None:
        env = dict(os.environ)
    out = copy.deepcopy(raw)

    tg = out.get("telegram")
    if isinstance(tg, dict) and not tg.get("bot_token"):
        token = env.get("TELEGRAM_BOT_TOKEN")
        if token:
            tg["bot_token"] = token

    llm = out.get("llm")
    if isinstance(llm, dict):
        for prov in llm.get("providers") or []:
            if isinstance(prov, dict) and not prov.get("api_key"):
                key = env.get(f"LLM_API_KEY_{_norm(str(prov.get('name', '')))}") or env.get("LLM_API_KEY")
                if key:
                    prov["api_key"] = key

    return out


def _self_check() -> None:
    base = {
        "telegram": {"bot_token": "", "allowed_user_ids": [1]},
        "llm": {"providers": [
            {"name": "open-router", "api_key": "", "model": "x"},
            {"name": "groq", "api_key": "kept", "model": "y"},
        ]},
    }

    # Blank secrets filled from env; provider-name normalization works.
    out = apply_env_overrides(base, {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "LLM_API_KEY_OPEN_ROUTER": "or-key",
    })
    assert out["telegram"]["bot_token"] == "123:abc"
    assert out["llm"]["providers"][0]["api_key"] == "or-key"
    assert out["llm"]["providers"][1]["api_key"] == "kept"      # explicit value wins

    # Generic LLM_API_KEY fallback for any blank provider.
    out2 = apply_env_overrides(base, {"LLM_API_KEY": "shared"})
    assert out2["llm"]["providers"][0]["api_key"] == "shared"

    # No env → unchanged; input never mutated.
    out3 = apply_env_overrides(base, {})
    assert out3["telegram"]["bot_token"] == ""
    assert base["telegram"]["bot_token"] == "", "input dict must not be mutated"

    # Explicit file value is never overwritten by env.
    filled = {"telegram": {"bot_token": "file-token"}, "llm": {"providers": []}}
    assert apply_env_overrides(filled, {"TELEGRAM_BOT_TOKEN": "env"})["telegram"]["bot_token"] == "file-token"

    print("env_overrides self-check: OK")


if __name__ == "__main__":
    _self_check()
