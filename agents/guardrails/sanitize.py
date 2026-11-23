"""Text sanitising for untrusted source text and for model-produced free text.

WHAT: `sanitize_source_text` (before text reaches the model) and `sanitize_model_text` (before
model output is stored/displayed).
WHERE: Lead Research `search` node (sources) and `validate` node (industry, signals,
rationales); reusable by any agent that ingests external text.
WHY: Invisible characters (zero-width, bidi controls, Unicode "tag" characters) can hide
instructions from human reviewers while the model still reads them, so they are removed.
Source text is sanitised ONCE at ingestion and the sanitised form is what both the model and
quote verification see, so evidence stays consistent. Model free text is also stored and later
rendered (Streamlit) and ingested (wiki), so markup and links are neutralised: a second-order
injection path. Visible text is otherwise left alone.
"""
import re
import unicodedata

# C0/C1 controls (keeping \n, \t), zero-width + joiners, bidi overrides/isolates, soft hyphen,
# BOM, and the Unicode "tag" block (U+E0000-E007F) used for ASCII smuggling.
_INVISIBLE = re.compile(
    r"[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u206f\ufeff\U000e0000-\U000e007f]"
)
_SPACES = re.compile(r"[ \t]{2,}")
_BLANKS = re.compile(r"\n{3,}")
_HTML = re.compile(r"<[^>\n]{0,200}>")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


_TAG_CHARS = re.compile(r"[\U000e0000-\U000e007f]")


def contains_tag_characters(text: str) -> bool:
    """True if the text carries Unicode 'tag' characters: invisible, and in web text almost only
    ever used to smuggle instructions, so their presence is itself worth a flag."""
    return bool(_TAG_CHARS.search(text))


def sanitize_source_text(text: str, max_chars: int | None = None) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _BLANKS.sub("\n\n", text).strip()
    return text[:max_chars] if max_chars is not None else text


def sanitize_model_text(text: str | None, max_len: int) -> str | None:
    if not text:
        return None
    text = sanitize_source_text(text)
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)  # keep the visible link text, drop the target
    text = _HTML.sub("", text)
    text = _URL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()[:max_len].strip()
    return text or None
