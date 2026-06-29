"""
Telegram-safe text formatting utilities.

Converts model Markdown/HTML-ish output into the small HTML subset accepted by
Telegram Bot API ParseMode.HTML. Normal browser HTML such as <p>, <ul>, <li>,
<div>, <br>, and <table> is converted to plain text or escaped.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urlparse

import mistune
from core.output_filter import sanitize_user_visible_text

TELEGRAM_MAX_TEXT_LENGTH: Final[int] = 4096
TELEGRAM_SAFE_TEXT_LENGTH: Final[int] = 3800

_BLOCK_TAGS: Final[set[str]] = {
    "address", "article", "aside", "div", "footer", "header", "main",
    "nav", "p", "section", "tr",
}
_LIST_CONTAINER_TAGS: Final[set[str]] = {"ul", "ol", "menu"}
_TABLE_CELL_TAGS: Final[set[str]] = {"td", "th"}


def escape_html_text(text: str) -> str:
    """Escape arbitrary text for Telegram HTML."""
    return html.escape(text or "", quote=False)


def _strip_think_blocks(text: str) -> str:
    """Remove hidden reasoning blocks and common leaked narration."""
    return sanitize_user_visible_text(text)

def _is_safe_href(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https", "tg"}


def _clean_language(info: str | None) -> str:
    if not info:
        return ""
    lang = info.strip().split()[0]
    return re.sub(r"[^a-zA-Z0-9_+.-]", "", lang)[:40]


class _TelegramHTMLSanitizer(HTMLParser):
    """Allow only Telegram Bot API HTML tags; convert common HTML tags."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.stack: list[str] = []

    def get_html(self) -> str:
        while self.stack:
            self.parts.append(f"</{self.stack.pop()}>")
        return "".join(self.parts)

    def _append_newline(self, count: int = 1) -> None:
        current = "".join(self.parts)
        existing = len(current) - len(current.rstrip("\n"))
        missing = count - existing
        if missing > 0:
            self.parts.append("\n" * missing)

    @staticmethod
    def _attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {k.lower(): (v or "") for k, v in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = self._attrs_dict(attrs)

        if tag in _BLOCK_TAGS:
            self._append_newline(1)
            return
        if tag in _LIST_CONTAINER_TAGS:
            self._append_newline(1)
            return
        if tag == "li":
            self._append_newline(1)
            self.parts.append("• ")
            return
        if tag == "br":
            self._append_newline(1)
            return
        if tag in _TABLE_CELL_TAGS:
            self.parts.append(" | ")
            return

        if tag in {"b", "strong"}:
            self.parts.append("<b>")
            self.stack.append("b")
            return
        if tag in {"i", "em"}:
            self.parts.append("<i>")
            self.stack.append("i")
            return
        if tag in {"u", "ins"}:
            self.parts.append("<u>")
            self.stack.append("u")
            return
        if tag in {"s", "strike", "del"}:
            self.parts.append("<s>")
            self.stack.append("s")
            return
        if tag == "tg-spoiler" or (tag == "span" and attr.get("class") == "tg-spoiler"):
            self.parts.append("<tg-spoiler>")
            self.stack.append("tg-spoiler")
            return
        if tag == "a":
            href = html.unescape(attr.get("href", "")).strip()
            if _is_safe_href(href):
                self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
                self.stack.append("a")
            return
        if tag == "code":
            class_name = attr.get("class", "").strip()
            if re.fullmatch(r"language-[a-zA-Z0-9_+.-]{1,40}", class_name):
                self.parts.append(f'<code class="{html.escape(class_name, quote=True)}">')
            else:
                self.parts.append("<code>")
            self.stack.append("code")
            return
        if tag == "pre":
            self.parts.append("<pre>")
            self.stack.append("pre")
            return
        if tag == "blockquote":
            expandable = " expandable" if "expandable" in attr or any(a[0].lower() == "expandable" for a in attrs) else ""
            self.parts.append(f"<blockquote{expandable}>")
            self.stack.append("blockquote")
            return
        if tag == "tg-emoji":
            emoji_id = attr.get("emoji-id", "").strip()
            if emoji_id.isdigit():
                self.parts.append(f'<tg-emoji emoji-id="{emoji_id}">')
                self.stack.append("tg-emoji")
            return
        if tag == "tg-time":
            unix = attr.get("unix", "").strip()
            fmt = attr.get("format", "").strip()
            if unix.isdigit() and re.fullmatch(r"[rw]?[dD]?[tT]?", fmt):
                fmt_attr = f' format="{fmt}"' if fmt else ""
                self.parts.append(f'<tg-time unix="{unix}"{fmt_attr}>')
                self.stack.append("tg-time")
            return

        self.parts.append(html.escape(self.get_starttag_text() or f"<{tag}>", quote=False))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self._append_newline(1)
            return
        if tag in _BLOCK_TAGS or tag in _LIST_CONTAINER_TAGS:
            self._append_newline(1)
            return
        self.parts.append(html.escape(self.get_starttag_text() or f"<{tag}/>", quote=False))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if tag in _BLOCK_TAGS:
            self._append_newline(2)
            return
        if tag in _LIST_CONTAINER_TAGS:
            self._append_newline(1)
            return
        if tag == "li":
            self._append_newline(1)
            return
        if tag in _TABLE_CELL_TAGS:
            self.parts.append(" ")
            return

        close_map = {
            "strong": "b", "b": "b", "em": "i", "i": "i",
            "ins": "u", "u": "u", "strike": "s", "del": "s", "s": "s",
            "span": "tg-spoiler", "tg-spoiler": "tg-spoiler", "a": "a",
            "code": "code", "pre": "pre", "blockquote": "blockquote",
            "tg-emoji": "tg-emoji", "tg-time": "tg-time",
        }
        normalized = close_map.get(tag)
        if not normalized:
            self.parts.append(html.escape(f"</{tag}>", quote=False))
            return

        if normalized in self.stack:
            while self.stack:
                open_tag = self.stack.pop()
                self.parts.append(f"</{open_tag}>")
                if open_tag == normalized:
                    break

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        if name in {"lt", "gt", "amp", "quot"}:
            self.parts.append(f"&{name};")
        else:
            self.parts.append(html.escape(html.unescape(f"&{name};"), quote=False))

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return


def sanitize_telegram_html(value: str) -> str:
    """Return HTML that uses only Telegram-supported tags/entities."""
    parser = _TelegramHTMLSanitizer()
    parser.feed(value or "")
    parser.close()
    sanitized = parser.get_html()
    sanitized = re.sub(r"[ \t]+\n", "\n", sanitized)
    sanitized = re.sub(r"\n{4,}", "\n\n\n", sanitized)
    return sanitized.strip()


class TelegramHTMLRenderer(mistune.HTMLRenderer):
    """Mistune renderer that emits Telegram-safe HTML directly."""

    def paragraph(self, text: str) -> str:
        return f"{text}\n\n"

    def heading(self, text: str, level: int, **attrs: object) -> str:
        return f"<b>{text}</b>\n\n"

    def list(self, text: str, ordered: bool, **attrs: object) -> str:
        return f"{text}\n"

    def list_item(self, text: str) -> str:
        clean = text.strip()
        clean = re.sub(r"\n{2,}", "\n", clean)
        clean = clean.replace("\n", "\n  ")
        return f"• {clean}\n"

    def block_quote(self, text: str) -> str:
        return f"<blockquote>{text.strip()}</blockquote>\n\n"

    def strong(self, text: str) -> str:
        return f"<b>{text}</b>"

    def emphasis(self, text: str) -> str:
        return f"<i>{text}</i>"

    def codespan(self, text: str) -> str:
        return f"<code>{html.escape(html.unescape(text), quote=False)}</code>"

    def block_code(self, code: str, info: str | None = None) -> str:
        escaped = html.escape(code, quote=False)
        lang = _clean_language(info)
        if lang:
            return f'<pre><code class="language-{lang}">{escaped}</code></pre>\n\n'
        return f"<pre>{escaped}</pre>\n\n"

    def link(self, text: str, url: str, title: str | None = None) -> str:
        url = html.unescape(url).strip()
        if not _is_safe_href(url):
            return text
        return f'<a href="{html.escape(url, quote=True)}">{text}</a>'

    def image(self, text: str, url: str, title: str | None = None) -> str:
        url = html.unescape(url).strip()
        label = text or "image"
        if not _is_safe_href(url):
            return label
        return f'<a href="{html.escape(url, quote=True)}">🖼 {label}</a>'

    def thematic_break(self) -> str:
        return "───\n"

    def linebreak(self) -> str:
        return "\n"

    def softbreak(self) -> str:
        return "\n"

    def inline_html(self, html_text: str) -> str:
        return sanitize_telegram_html(html_text)

    def block_html(self, html_text: str) -> str:
        return sanitize_telegram_html(html_text) + "\n\n"


_markdown_renderer = mistune.create_markdown(renderer=TelegramHTMLRenderer(escape=True))


def render_markdown_to_html(markdown_text: str) -> str:
    """Convert arbitrary model Markdown into Telegram-safe HTML."""
    if not markdown_text:
        return ""
    normalized = _strip_think_blocks(markdown_text)
    rendered = _markdown_renderer(normalized)
    return sanitize_telegram_html(rendered)


def telegram_html_to_plain_text(value: str) -> str:
    """Convert Telegram HTML-ish text to readable plain text fallback."""
    value = sanitize_telegram_html(value)
    value = re.sub(r"<a\s+href=\"([^\"]+)\">(.*?)</a>", r"\2 (\1)", value, flags=re.DOTALL)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value).strip()


def split_text_for_telegram(text: str, max_length: int = TELEGRAM_SAFE_TEXT_LENGTH) -> list[str]:
    """Split text into Telegram-sized chunks without hard truncation."""
    if max_length <= 0:
        raise ValueError("max_length must be positive")
    text = text or ""
    if len(text) <= max_length:
        return [text] if text else []

    parts: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(remaining) <= max_length:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, max_length)
        if cut < max_length // 3:
            cut = remaining.rfind("\n", 0, max_length)
        if cut < max_length // 3:
            cut = remaining.rfind(" ", 0, max_length)
        if cut < max_length // 3:
            cut = max_length
        parts.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    return [part for part in parts if part]


def tail_text_for_telegram(text: str, max_length: int = TELEGRAM_SAFE_TEXT_LENGTH) -> str:
    """Return the latest tail of text that fits into one Telegram draft."""
    if len(text) <= max_length:
        return text
    tail = text[-max_length:]
    cut = tail.find("\n")
    if 0 <= cut < max_length // 3:
        tail = tail[cut + 1 :]
    return "…" + tail.strip()


def truncate_for_telegram(text: str, max_length: int = TELEGRAM_MAX_TEXT_LENGTH) -> str:
    """Backward-compatible truncation helper for one-off messages."""
    if max_length < 4:
        raise ValueError("max_length must be >= 4")
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "…"


def format_action_blockquote(tool: str, target: str) -> str:
    """Format a tool action as Telegram-safe HTML."""
    escaped_tool = escape_html_text(tool)
    escaped_target = escape_html_text(target)
    return f"<blockquote>⚙️ [{escaped_tool}] -&gt; {escaped_target}</blockquote>\n"


def format_error_message(error: str) -> str:
    """Format an error string as Telegram-safe HTML."""
    escaped_error = escape_html_text(error)
    return f"⚠️ <b>Error:</b> {escaped_error}"
