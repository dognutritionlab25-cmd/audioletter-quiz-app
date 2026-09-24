"""Render operator-supplied Markdown without executing embedded HTML."""

from html import escape

import bleach
import markdown
from markupsafe import Markup


_TAGS = {"p", "br", "strong", "em", "h1", "h2", "h3", "h4", "h5", "h6",
         "blockquote", "ul", "ol", "li", "code", "pre", "hr"}


def render_audioletter_text(value):
    # Escape raw HTML before parsing, then allow only parser-generated formatting.
    source = escape(value or "", quote=False)
    rendered = markdown.markdown(source, extensions=["nl2br"])
    return Markup(bleach.clean(rendered, tags=_TAGS, attributes={}, strip=True))
