from __future__ import annotations

_EMBED_AUDIO_CSS = """
<style id="skkkk-embed">
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
<style id="skkkk-embed">
  html, body { background: transparent !important; margin: 0 !important; padding: 0 !important; }
  h1, .meta { display: none !important; }
  .card {
    border: none !important;
    border-radius: 0 !important;
    padding: 0 !important;
    margin: 0 0 18px !important;
    background: transparent !important;
    box-shadow: none !important;
  }
  h2 { font-size: 1.05rem !important; margin-top: 0 !important; }
</style>
"""


def apply_embed_mode(html: str, *, kind: str) -> str:
    css = _EMBED_AUDIO_CSS if kind == "audio" else _EMBED_DOCUMENT_CSS
    if "</head>" in html:
        return html.replace("</head>", css + "</head>", 1)
    return css + html
