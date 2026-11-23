"""Nonce-delimited wrapping of untrusted text for prompts.

WHAT: `new_boundary()` and `wrap_untrusted()` render external text inside a tag whose name
includes a random per-call token.
WHERE: Prompt builders (Lead Research `synthesize`; later the CRM, wiki and handoff agents).
WHY: A fixed tag (`</untrusted_source>`) can be forged by the very page we are wrapping, which
lets it "close" the data region and add text that looks like instructions. A random boundary
the page cannot know removes the problem without altering the legitimate text. If a source
somehow contains the boundary, the caller simply draws a new one (see `boundary_for`).
"""
import secrets
from collections.abc import Iterable


def new_boundary() -> str:
    return secrets.token_hex(8)


def boundary_for(texts: Iterable[str]) -> str:
    """A boundary that appears in none of `texts` (a collision is ~impossible but checked)."""
    texts = list(texts)
    while True:
        b = new_boundary()
        if not any(b in t for t in texts):
            return b


def _attr(value: str) -> str:
    return value.replace('"', "%22").replace("<", "%3C").replace(">", "%3E").replace("\n", " ")


def tag_name(boundary: str) -> str:
    return f"untrusted_source_{boundary}"


def wrap_untrusted(boundary: str, source_id: str, url: str, text: str) -> str:
    t = tag_name(boundary)
    return f'<{t} id="{_attr(source_id)}" url="{_attr(url)}">\n{text}\n</{t}>'
