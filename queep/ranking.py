"""Rank ready references for a generous list. Weights are the initial calibration."""

import math
import re

from queep.textnorm import fold

# Window returned to the client. Inside the 40–80 target; raise after eval/recall.py.
RESULT_LIMIT = 60
# Always keep this many ready items when the library has them, even past the gap.
MIN_RESULTS = 12
# Contradiction checker only. The tail is returned unverified so the prompt stays ~19.5 KB.
HEAD_LIMIT = 8
# Keep a hit when its score is at least (1 - gap) times the best score.
# Wide on purpose: reciprocal-rank scores decay slowly. Calibrate with eval/recall.py.
TAIL_RELATIVE_GAP = 0.55
COLOR_FAMILIES = {
    "roxo": ("roxo", "roxa", "roxos", "roxas", "purple", "violeta", "violet", "lilas", "lilac"),
    "verde": ("verde", "verdes", "green"),
    "azul": ("azul", "azuis", "blue"),
    "vermelho": ("vermelho", "vermelha", "vermelhos", "vermelhas", "red"),
    "amarelo": ("amarelo", "amarela", "amarelos", "amarelas", "yellow"),
    "laranja": ("laranja", "laranjas", "orange"),
    "rosa": ("rosa", "rosas", "pink"),
    "preto": ("preto", "preta", "pretos", "pretas", "black"),
    "branco": ("branco", "branca", "brancos", "brancas", "white"),
    "cinza": ("cinza", "cinzas", "gray", "grey"),
}
_COLOR_LOOKUP = {
    fold(name): family for family, names in COLOR_FAMILIES.items() for name in names
}
_ONLY_VIDEO = re.compile(
    r"\b(?:somente|apenas|so|only)\b(?:\s+\w+){0,3}\s+\b(?:videos?)\b",
    re.I,
)
_NEGATION = re.compile(r"\b(?:nao|sem|not|without|nunca)\s+([a-z0-9]+)")


def floats(value):
    if value is None:
        return None
    try:
        return [float(item) for item in value]
    except (TypeError, ValueError):
        return None


def cosine(query, embedding):
    if not query or not embedding or len(query) != len(embedding):
        return None
    dot = norm_query = norm_embedding = 0.0
    for left, right in zip(query, embedding, strict=True):
        dot += left * right
        norm_query += left * left
        norm_embedding += right * right
    if norm_query <= 0 or norm_embedding <= 0:
        return None
    return dot / math.sqrt(norm_query * norm_embedding)


def cut_tail(ranked):
    """Drop the weak tail relative to the best score, after the minimum count."""
    if not ranked:
        return []
    best = ranked[0][0]
    floor = best * (1.0 - TAIL_RELATIVE_GAP) if best > 0 else best
    kept = []
    for item in ranked:
        if len(kept) >= RESULT_LIMIT:
            break
        if item[0] >= floor or len(kept) < MIN_RESULTS:
            kept.append(item)
            continue
        break
    return kept


def vector_order(records, vector):
    scored = []
    for data in records:
        if data.get("archived_at") is not None:
            continue
        score = cosine(vector, floats(data.get("embedding")))
        if score is None:
            continue
        scored.append((score, data.get("id") or "", data))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored



def order_ready(records, vector):
    return [item[2] for item in cut_tail(vector_order(records, vector))]


def _blob(reference):
    analysis = reference.analysis
    parts = []
    if analysis:
        parts.extend([
            analysis.summary, analysis.headline, " ".join(analysis.appearance),
            " ".join(analysis.topics), " ".join(getattr(analysis, "colors", []) or []),
            " ".join(getattr(analysis, "on_screen_text", []) or []),
            getattr(analysis, "brand", "") or "",
        ])
    return fold(" ".join(part for part in parts if part))


def _families_in(text):
    found = set()
    folded = fold(text)
    for name, family in _COLOR_LOOKUP.items():
        if re.search(rf"\b{re.escape(name)}\b", folded):
            found.add(family)
    return found


def strict_video_query(query):
    return bool(_ONLY_VIDEO.search(fold(query)))


def video_like(reference):
    if reference.kind == "video":
        return True
    url = fold(reference.url or "")
    if any(host in url for host in ("youtube.com", "youtu.be", "vimeo.com")):
        return True
    types = getattr(reference.analysis, "ui_types", None) or []
    return "vídeo" in types or "video" in {fold(item) for item in types}


def contradicting_ids(query, references):
    """Deterministic head filter: wrong color, negated color, explicit 'only videos'."""
    wanted = _families_in(query)
    negated = set()
    for match in _NEGATION.finditer(fold(query)):
        negated |= _families_in(match.group(1))
    only_video = strict_video_query(query)
    dropped = []
    for reference in references:
        present = _families_in(_blob(reference))
        if wanted and present and wanted.isdisjoint(present):
            dropped.append(reference.id)
            continue
        if negated and negated & present:
            dropped.append(reference.id)
            continue
        if only_video and not video_like(reference):
            dropped.append(reference.id)
    return dropped
