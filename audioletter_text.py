"""Render operator-supplied Markdown without executing embedded HTML."""

from html import escape
import re

import bleach
import markdown
from markupsafe import Markup


_TAGS = {"p", "br", "strong", "em", "h1", "h2", "h3", "h4", "h5", "h6",
         "blockquote", "ul", "ol", "li", "code", "pre", "hr"}


def render_audioletter_text(value):
    # Escape raw HTML before parsing, then allow only parser-generated formatting.
    # Notion copy/paste can insert nonbreaking and zero-width spaces before # headings.
    plain = ((value or "").replace("\r\n", "\n").replace("\r", "\n")
             .replace("\u00a0", " ").replace("\u200b", "").replace("\ufeff", ""))
    plain = re.sub(r"(?m)^[ \t]+(?=#{1,6}(?:[ \t]|$))", "", plain)
    source = escape(plain, quote=False)
    rendered = markdown.markdown(source, extensions=["nl2br"])
    return Markup(bleach.clean(rendered, tags=_TAGS, attributes={}, strip=True))
