"""
User-visible output sanitization.

The model can still produce hidden reasoning in normal ``content`` despite
prompting or provider-side reasoning fields. Because streamed channel drafts
cannot be taken back reliably, this filter buffers a model turn and releases
only sanitized final text.
"""

from __future__ import annotations

import html
import re
from typing import Final

_REASONING_TAGS: Final[str] = r"(?:think|thought|reasoning|reflection|internal|analysis)"

_REASONING_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?is)<{_REASONING_TAGS}\b[^>]*>.*?(?:</{_REASONING_TAGS}\s*>|$)"
)
_REASONING_CLOSE_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?is)</{_REASONING_TAGS}\s*>"
)

_INTERNAL_NARRATION_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^\s*(?:[-*>\u2022]\s*)?
    (?:
        (?:the\s+)?user\s+(?:asked|asks|is\s+asking|has\s+sent|sent|wants|said)\b
        | now\s+the\s+user\b
        | i\s+(?:need|should|must|will|used|have|already|can)\b.*\b(?:answer|respond|reply|provide|check|read|use|call|output|final|tool|file|conversation)\b
        | so\s+i\s+(?:need|should|will|must)\b
        | let\s+me\s+(?:check|read|see|start|look|provide|inspect)\b
        | looking\s+at\b
        | from\s+the\s+(?:shell|tool|output|conversation|context)\b
        | given\s+the\b
        | however,?\s+(?:the\s+)?(?:tool|conversation|user|output)\b
        | this\s+(?:appears|seems)\s+to\s+be\b
        | the\s+phrasing\s+is\b
        | reading\s+carefully\b
        | but\s+wait\b
        | thus\s+i\s+will\b
        | therefore,?\s+(?:the\s+answer|i\s+will|i\s+should)\b
        | yes\.\s*$
        | tags?\.\s+just\s+give\b
        | pertanyaan\s+user\b
        | user\s+bertanya\b
        | pengguna\s+(?:bertanya|meminta)\b
        | saya\s+(?:perlu|harus|sebaiknya|akan|memiliki)\b.*\b(?:menjawab|memberikan|menyampaikan|membaca|memeriksa|menggunakan|mengakui|mengubah)\b
        | sekarang\s+saya\s+akan\b
        | berdasarkan\s+(?:file|output|konteks|hasil)\b
        | dalam\s+konteks\s+ini\b
        | membaca\s+file\b
        | melihat\s+output\b
        | aturan\s+operasi\b
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)

_PREAMBLE_CONTINUATION_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^\s*
    (?:
        [-*\u2022]\s+
        | \d+[.)]\s+
        | [A-Za-z0-9_./-]+\s*[:=]\s*
        | (?:that|this|it|there|but|however|given|thus|therefore)\b
        | (?:maka|namun|jadi|sehingga|ini|itu)\b
    )
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)


def _strip_reasoning_tags(text: str) -> str:
    """Remove explicit reasoning tags, including malformed unclosed blocks."""
    if not text:
        return ""
    unescaped = html.unescape(text)
    unescaped = _REASONING_BLOCK_RE.sub("", unescaped)
    return _REASONING_CLOSE_RE.sub("", unescaped)


def _is_internal_narration(paragraph: str) -> bool:
    """Best-effort detection for tagless model meta-analysis."""
    stripped = paragraph.strip()
    if not stripped:
        return False
    if _INTERNAL_NARRATION_RE.search(stripped):
        return True

    lowered = stripped.lower()
    return (
        "chain-of-thought" in lowered
        or "internal reasoning" in lowered
        or "hidden reasoning" in lowered
        or "final user-facing response" in lowered
        or "do not include any internal reasoning" in lowered
    )


def _is_preamble_continuation(paragraph: str) -> bool:
    stripped = paragraph.strip()
    if not stripped:
        return True
    return bool(_PREAMBLE_CONTINUATION_RE.search(stripped)) or _is_internal_narration(stripped)


def sanitize_user_visible_text(text: str) -> str:
    """Return text that is safe to show to the user.

    This removes both explicit reasoning blocks and common tagless narration
    patterns seen from reasoning models. The tagless pass is intentionally
    conservative and mostly targets leading analysis preambles.
    """
    cleaned = _strip_reasoning_tags(text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""

    paragraphs = re.split(r"\n\s*\n", cleaned)
    visible: list[str] = []
    suppressing_preamble = False

    for paragraph in paragraphs:
        candidate = paragraph.strip()
        if not candidate:
            continue

        if _is_internal_narration(candidate):
            suppressing_preamble = not visible
            continue

        if suppressing_preamble and _is_preamble_continuation(candidate):
            continue

        suppressing_preamble = False
        visible.append(candidate)

    return "\n\n".join(visible).strip()


class UserVisibleStreamFilter:
    """Stateful safety gate for streamed model text.

    ``feed`` buffers chunks and deliberately returns no text. ``flush`` returns
    the sanitized final text for the model turn.
    """

    def __init__(self) -> None:
        self._buffer: str = ""

    def feed(self, chunk: str) -> list[str]:
        if chunk:
            self._buffer += chunk
        return []

    def flush(self) -> list[str]:
        visible = sanitize_user_visible_text(self._buffer)
        self._buffer = ""
        return [visible] if visible else []
