"""Режимы встраивания HTML-отчётов (прозрачный фон, без шапки) и скрытие элементов управления по роли пользователя."""

from __future__ import annotations

_EMBED_AUDIO_CSS = """
<style id="primaryanalitics-embed">
  html, body { background: transparent !important; margin: 0 !important; }
  header, main { max-width: none !important; padding: 0 !important; }
  header > h1 { display: none !important; }
  .navbar, nav.navbar { display: none !important; }
  .container-fluid { max-width: none !important; padding-left: 0 !important; padding-right: 0 !important; }
  .row > .col-lg-4 { display: none !important; }
  .col-lg-8 { width: 100% !important; flex: 0 0 100% !important; max-width: 100% !important; }
  h1.h3 { font-size: 1.1rem !important; }
  .summary {
    border: none !important;
    border-radius: 0 !important;
    padding: 0 0 16px !important;
    margin: 0 0 16px !important;
    background: transparent !important;
    box-shadow: none !important;
  }
  .rename {
    border: none !important;
    border-radius: 0 !important;
    padding: 0 !important;
    margin: 0 0 16px !important;
    background: transparent !important;
    box-shadow: none !important;
  }
  .rename label { align-items: start !important; }
  textarea[data-rename-src], textarea.auto-grow {
    width: 100%;
    min-height: 2.4em;
    resize: none;
    overflow: hidden;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .card { box-shadow: none !important; }
</style>
"""

_EMBED_DOCUMENT_CSS = """
<style id="primaryanalitics-embed">
  html, body { background: transparent !important; margin: 0 !important; padding: 0 !important; }
  header > h1 { display: none !important; }
  header, main { max-width: none !important; padding: 0 !important; }
  .summary {
    border: none !important;
    border-radius: 0 !important;
    padding: 0 0 16px !important;
    margin: 0 0 16px !important;
    background: transparent !important;
    box-shadow: none !important;
  }
  .export-panel, .source-collapse {
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
  }
</style>
"""


def apply_embed_mode(html: str, *, kind: str) -> str:
    css = _EMBED_AUDIO_CSS if kind == "audio" else _EMBED_DOCUMENT_CSS
    if "</head>" in html:
        return html.replace("</head>", css + "</head>", 1)
    return css + html


def apply_role_chrome(html: str, role: str) -> str:
    snippet = f"<script>window.PRIMARY_ANALITICS_ROLE={role!r};</script>"
    if role == "user":
        snippet += (
            '<style id="primaryanalitics-role-chrome">'
            "#rebuild-summary,#recalc,#reindex{display:none!important}"
            "</style>"
        )
    if "</head>" in html:
        return html.replace("</head>", snippet + "</head>", 1)
    return snippet + html
