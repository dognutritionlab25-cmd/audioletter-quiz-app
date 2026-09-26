"""Render operator-supplied Markdown without executing embedded HTML."""

from html import escape
import re

import bleach
import markdown
from markupsafe import Markup


_TAGS = {"p", "br", "strong", "em", "h1", "h2", "h3", "h4", "h5", "h6",
         "blockquote", "ul", "ol", "li", "code", "pre", "hr"}


def _markdown_source(value):
    # Escape raw HTML before parsing, then allow only parser-generated formatting.
    # Notion copy/paste can insert nonbreaking and zero-width spaces before # headings.
    plain = ((value or "").replace("\r\n", "\n").replace("\r", "\n")
             .replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", ""))
    plain = re.sub(r"(?m)^[ \t]+(?=#{1,6}(?:[ \t]|$))", "", plain)
    return escape(plain, quote=False)


def _safe_markdown(value):
    rendered = markdown.markdown(_markdown_source(value), extensions=["nl2br"])
    return Markup(bleach.clean(rendered, tags=_TAGS, attributes={}, strip=True))


def render_audioletter_text(value):
    return _safe_markdown(value)


def render_audioletter_title(value):
    """Render a title inline so Markdown formatting does not create a paragraph in a heading."""
    source = _markdown_source(value)
    # Some imported titles contain spaces immediately before the closing emphasis
    # marker (for example, ``**R001-E ...  **``), which Markdown treats as text.
    source = re.sub(r"(\*\*|__)(.*?)[ \t]+\1", r"\1\2\1", str(source))
    rendered = markdown.markdown(source, extensions=["nl2br"])
    rendered = bleach.clean(rendered, tags=_TAGS, attributes={}, strip=True)
    match = re.fullmatch(r"<p>(.*)</p>", rendered, flags=re.DOTALL)
    return Markup(match.group(1) if match else rendered)
