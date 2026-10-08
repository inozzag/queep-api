"""What a saved item is, in the words a search would use.

`queep-interpretation-v8` (PR #9, `queep/backfill.py`) replaces `document()`
and `ANALYSIS_VERSION` in `queep/ai.py`. Keep this module and call
`portrait_preamble()` on the first lines of that function. Do not add another
version string here.

`interpretation_schema()` leaves `portrait` optional. If v8 marks every
property required, keep `portrait` out of `required` or append `PORTRAIT_PROMPT`
to that prompt.
"""

from urllib.parse import urlsplit


def _clip(text, byte_limit):
    return text.encode()[:byte_limit].decode("utf-8", errors="ignore")

_SOCIAL = (
    "instagram.com", "x.com", "twitter.com", "pinterest.com",
    "dribbble.com", "behance.net",
)
PORTRAIT_PROMPT = """
portrait: uma ou duas frases começando por "Isto é", com o tipo real
(site, blog, landing, ferramenta, post, foto, vídeo, áudio, nota, anúncio
ou perfil) e para que serve. Vazio somente quando não houver material.
"""


def item_phrase(reference):
    """Kind a person would type: um site, um vídeo, um post."""
    if reference.kind == "image":
        return "uma foto"
    if reference.kind == "audio":
        return "um áudio"
    if reference.kind == "video" or reference.media:
        return "um vídeo"
    if reference.kind == "note":
        return "uma nota"
    host = ""
    if reference.url:
        host = (urlsplit(reference.url).hostname or "").lower().removeprefix("www.")
    if host in ("youtu.be", "youtube.com", "m.youtube.com", "vimeo.com") or host.endswith(
            (".youtube.com", ".vimeo.com")):
        return "um vídeo"
    if reference.source and reference.source.content_kind == "profile":
        return "um perfil"
    if reference.source and reference.source.content_kind == "page":
        return "um site"
    if reference.images and any(image.preview_kind == "video" for image in reference.images):
        return "um vídeo"
    if any(host == name or host.endswith("." + name) for name in _SOCIAL):
        return "um post"
    return "um link"


def portrait_line(reference, analysis):
    written = (getattr(analysis, "portrait", None) or "").strip()
    if written:
        return _clip(written, 500)
    if reference.kind == "image" and analysis.appearance:
        return _clip("Isto é uma foto de " + analysis.appearance[0].rstrip(".") + ".", 500)
    summary = (analysis.summary or "").strip()
    phrase = item_phrase(reference)
    if summary:
        return _clip(f"Isto é {phrase}. {summary}", 500)
    return f"Isto é {phrase}."


def _labeled(name, values):
    items = [item.strip() for item in values if item and item.strip()]
    if not items:
        return ""
    return name + ": " + _clip("; ".join(items), 700)


def portrait_preamble(reference, analysis):
    """Lines v8's document() should keep at the top. No coverage or limitations."""
    title = (reference.title or "").strip()
    return "\n".join(part for part in [
        portrait_line(reference, analysis),
        "Tipo: " + item_phrase(reference),
        _labeled("Serve para", analysis.usefulness),
        _labeled("Assuntos", analysis.topics),
        _labeled("Nomes", analysis.entities),
        _labeled("Aparência", analysis.appearance),
        ("Título: " + _clip(title, 300)) if title else "",
    ] if part)
