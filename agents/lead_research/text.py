"""URL and text normalisation used to verify evidence.

WHAT: Canonicalises URLs/domains and text so an LLM-supplied URL or quote can be matched
against what was actually fetched.
WHERE: Used by source building (`nodes.py`) and evidence verification (`validate.py`).
WHY: LLMs re-type URLs (trailing slash, `www.`, tracking params) and quotes (curly quotes,
collapsed whitespace). Matching on normalised forms rejects fabrication without rejecting
harmless formatting differences. (Source-text sanitising now lives in `agents/guardrails/sanitize.py`, Spec 006.)
"""
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_TRACKING = {"gclid", "fbclid", "mc_cid", "mc_eid"}
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                         "–": "-", "—": "-", " ": " "})


def normalize_url(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in _TRACKING)
    out = f"{(parts.scheme or 'https').lower()}://{host}{parts.path.rstrip('/')}"
    return out + (f"?{urlencode(query)}" if query else "")


def host_of(url: str) -> str:
    return (urlsplit(normalize_url(url)).hostname or "").lower()


def normalize_domain(value: str) -> str:
    return host_of(value)


def on_domain(host: str, domain: str | None) -> bool:
    return bool(domain) and (host == domain or host.endswith("." + domain))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip().casefold()


def quote_in_text(quote: str, normalized_text: str) -> bool:
    """`normalized_text` must already be `normalize_text(source)` (computed once per source)."""
    q = normalize_text(quote)
    return len(q) > 0 and q in normalized_text

