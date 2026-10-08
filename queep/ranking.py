"""Rank ready references for a generous list. Weights are the initial calibration."""

import math
import re

from queep.textnorm import SHORT_TOKENS, STOPWORDS, fold, tokenize

# Window returned to the client. Inside the 40–80 target; raise after eval/recall.py.
RESULT_LIMIT = 60
# Always keep this many ready items when the library has them, even past the gap.
MIN_RESULTS = 12
# Contradiction checker only. The tail is returned unverified.
HEAD_LIMIT = 8
# Keep a hit when its score is at least (1 - gap) times the best score.
# Used for queries that are neither a short theme nor a color constraint.
TAIL_RELATIVE_GAP = 0.55
# Short themes ("interface", "web design", "mobile"): 1–2 content words, no color.
# The fused head stays in order. Extra rows have to clear a cosine floor; an RRF
# gap does not, because k=60 keeps almost every fusion score within 15% of the best.
BROAD_TOKEN_LIMIT = 2
FUSED_PROTECT = 10
# Neighbor stays when cosine >= max(absolute, relative * best cosine).
# 0.82 drops a shelf that has fallen off the best neighbor; 0.40 rejects a query
# that never lands on this library. Cap keeps a flat-but-real theme inside 20–45.
BROAD_COSINE_RELATIVE = 0.82
BROAD_COSINE_FLOOR = 0.40
BROAD_RESULT_CAP = 40
# No lexical support (or the best cosine misses the absolute floor): short list.
BROAD_MISS_LIMIT = 8
# "Log in" / "Sign up" on most pages is chrome. A rare page term still counts.
CHROME_DF_MAX = 8
_CHROME_FIELDS = frozenset({"source_text", "transcript"})
# Color queries keep only rows with the color in v8 fields or, on older items,
# in appearance and the other write-up fields. No such row: a short near-miss list.
SPECIFIC_MISS_LIMIT = 5
_EVIDENCE_FIELDS = (
    "colors", "brand", "appearance", "summary", "headline", "title",
    "topics", "entities", "on_screen_text", "text", "ui_types",
)
RRF_K = 60
VECTOR_WEIGHT = 1.0
KEYWORD_WEIGHT = 1.25
EXPANSION_WEIGHT = 0.70
BM25_K1 = 1.2
BM25_B = 0.75
# source text and transcript carry scraped chrome ("Log in Sign up"), so they weigh less.
FIELD_WEIGHTS = {
    "title": 2.2,
    "text": 1.6,
    "summary": 1.5,
    "headline": 1.8,
    "topics": 1.7,
    "entities": 1.2,
    "appearance": 1.8,
    "usefulness": 0.6,
    "ui_types": 1.9,
    "colors": 2.0,
    "brand": 1.5,
    "on_screen_text": 1.4,
    "hook": 1.4,
    "style": 1.1,
    "transcript_summary": 1.0,
    "source_text": 0.35,
    "transcript": 0.35,
}

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


def cut_tail(ranked, gap=None, minimum=None):
    """Drop the weak tail relative to the best score, after the minimum count."""
    if not ranked:
        return []
    gap = TAIL_RELATIVE_GAP if gap is None else gap
    minimum = MIN_RESULTS if minimum is None else minimum
    best = ranked[0][0]
    floor = best * (1.0 - gap) if best > 0 else best
    kept = []
    for item in ranked:
        if len(kept) >= RESULT_LIMIT:
            break
        if item[0] >= floor or len(kept) < minimum:
            kept.append(item)
            continue
        break
    return kept


def content_words(query):
    """Query words that count toward broad vs specific, without stem duplicates."""
    words = []
    for raw in re.findall(r"[a-z0-9]+", fold(query)):
        if raw in STOPWORDS or (len(raw) < 3 and raw not in SHORT_TOKENS):
            continue
        words.append(raw)
    return words


def is_broad_query(query):
    """A short theme with no color. 'web design roxo' is not broad."""
    return not _families_in(query) and 0 < len(content_words(query)) <= BROAD_TOKEN_LIMIT


def _attribute_blob(record):
    """Color evidence. v8 colors/brand win; v7 items still match via appearance."""
    fields = field_text(record)
    return " ".join(fields[name] for name in _EVIDENCE_FIELDS if fields.get(name))


def evidences_colors(record, families):
    if not families:
        return True
    return not families.isdisjoint(_families_in(_attribute_blob(record)))


def _specific_window(scored, families):
    """Drop near-misses that never mention the asked color. Otherwise stay short."""
    matched = [item for item in scored if evidences_colors(item[2], families)]
    if matched:
        return [item[2] for item in matched[:RESULT_LIMIT]]
    return [item[2] for item in scored[:SPECIFIC_MISS_LIMIT]]


def _support_ids(records, query, expansions):
    """Ids that actually mention the theme. Ubiquitous page chrome does not."""
    wanted = set(tokenize(query))
    wanted.update(tokenize(" ".join(expansions or ())))
    if not wanted:
        return []
    strong = []
    chrome_counts = {}
    chrome_rows = []
    for record in records:
        identifier = record.get("id") or ""
        if not identifier:
            continue
        strong_hit = False
        chrome_hit = set()
        for name, value in field_text(record).items():
            found = wanted.intersection(tokenize(value))
            if not found:
                continue
            if name in _CHROME_FIELDS:
                chrome_hit |= found
            else:
                strong_hit = True
        if strong_hit:
            strong.append(identifier)
        elif chrome_hit:
            chrome_rows.append((identifier, chrome_hit))
            for token in chrome_hit:
                chrome_counts[token] = chrome_counts.get(token, 0) + 1
    rare = {token for token, count in chrome_counts.items() if count <= CHROME_DF_MAX}
    seen = set(strong)
    for identifier, found in chrome_rows:
        if identifier not in seen and found & rare:
            strong.append(identifier)
            seen.add(identifier)
    return strong


def _cosine_floor(best):
    if best <= 0:
        return BROAD_COSINE_FLOOR
    return max(BROAD_COSINE_FLOOR, best * BROAD_COSINE_RELATIVE)


def _broad_window(scored, cosine_rows, support_ids):
    """Fused head, then only neighbors that clear a cosine floor.

    The old loose fusion gap (0.85) plus a cosine backfill put the whole shelf
    back into the window and only reordered it. Order of the fused head stays.
    """
    fused_ids = [item[1] for item in scored]
    by_id = {item[1]: item[2] for item in scored}
    if not fused_ids:
        return []
    cosine_score = {row[1]: row[0] for row in cosine_rows}
    best = cosine_rows[0][0] if cosine_rows else 0.0
    floor = _cosine_floor(best)
    if not support_ids or best < BROAD_COSINE_FLOOR:
        return [by_id[identifier] for identifier in fused_ids[:BROAD_MISS_LIMIT]]
    passing = [identifier for identifier in fused_ids
               if cosine_score.get(identifier, 0.0) >= floor]
    if len(passing) > BROAD_RESULT_CAP:
        return [by_id[identifier] for identifier in fused_ids[:BROAD_RESULT_CAP]]
    head = fused_ids[:FUSED_PROTECT]
    seen = set(head)
    body = [identifier for identifier in passing if identifier not in seen]
    chosen = (head + body)[:BROAD_RESULT_CAP]
    return [by_id[identifier] for identifier in chosen]


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


def _analysis(record):
    analysis = record.get("analysis") or {}
    return analysis if isinstance(analysis, dict) else {}


def _join(value):
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item)
    return str(value or "")


def field_text(record):
    analysis = _analysis(record)
    source = record.get("source") or {}
    if not isinstance(source, dict):
        source = {}
    return {
        "title": record.get("title") or "",
        "text": record.get("text") or "",
        "summary": analysis.get("summary") or "",
        "headline": analysis.get("headline") or "",
        "topics": _join(analysis.get("topics")),
        "entities": _join(analysis.get("entities")),
        "appearance": _join(analysis.get("appearance")),
        "usefulness": _join(analysis.get("usefulness")),
        "ui_types": _join(analysis.get("ui_types")),
        "colors": _join(analysis.get("colors")),
        "brand": analysis.get("brand") or "",
        "on_screen_text": _join(analysis.get("on_screen_text")),
        "hook": analysis.get("hook") or "",
        "style": analysis.get("style") or "",
        "transcript_summary": analysis.get("transcript_summary") or "",
        "source_text": source.get("text") or "",
        "transcript": record.get("transcript") or "",
    }


def _weighted_tokens(record):
    counts = {}
    for name, text in field_text(record).items():
        weight = FIELD_WEIGHTS.get(name, 1.0)
        for token in tokenize(text):
            counts[token] = counts.get(token, 0.0) + weight
    return counts


def bm25_ranks(records, query):
    """Best-first ids with a positive score. Items that miss every term stay out."""
    tokens = tokenize(query)
    if not tokens or not records:
        return []
    docs = []
    for index, record in enumerate(records):
        identifier = record.get("id") or ""
        if identifier:
            docs.append((index, identifier, _weighted_tokens(record)))
    if not docs:
        return []
    average = sum(sum(counts.values()) for _, _, counts in docs) / len(docs) or 1.0
    document_frequency = {
        token: sum(1 for _, _, counts in docs if counts.get(token, 0) > 0) for token in set(tokens)
    }
    scored = []
    for index, identifier, counts in docs:
        score = 0.0
        length = sum(counts.values()) or 1.0
        for token in tokens:
            frequency = counts.get(token, 0.0)
            if frequency <= 0:
                continue
            df = document_frequency[token]
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            numerator = frequency * (BM25_K1 + 1)
            denominator = frequency + BM25_K1 * (1 - BM25_B + BM25_B * length / average)
            score += idf * numerator / denominator
        if score > 0:
            # Equal keyword scores keep the incoming order (vector rank).
            scored.append((score, index, identifier))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [identifier for _, _, identifier in scored]


def _rrf(rank_lists, weights):
    scores = {}
    for weight, ranking in zip(weights, rank_lists, strict=True):
        if weight <= 0:
            continue
        for rank, identifier in enumerate(ranking, start=1):
            scores[identifier] = scores.get(identifier, 0.0) + weight / (RRF_K + rank)
    return scores


def fuse_order(records, vector, query, expansions=()):
    """Reciprocal rank fusion of the vector list with keyword and expansion lists."""
    vectors = vector_order(records, vector)
    by_id = {item[1]: item[2] for item in vectors}
    vector_ids = [item[1] for item in vectors]
    active = [item[2] for item in vectors]
    keyword_ids = bm25_ranks(active, query) if query else []
    expansion_ids = bm25_ranks(active, " ".join(expansions)) if expansions else []
    fused = _rrf(
        [vector_ids, keyword_ids, expansion_ids],
        [VECTOR_WEIGHT, KEYWORD_WEIGHT if keyword_ids else 0.0,
         EXPANSION_WEIGHT if expansion_ids else 0.0],
    )
    scored = [(fused.get(identifier, 0.0), identifier, by_id[identifier])
              for identifier in vector_ids]
    scored.sort(key=lambda item: (-item[0], item[1]))
    families = _families_in(query)
    if families:
        return _specific_window(scored, families)
    if is_broad_query(query):
        return _broad_window(scored, vectors, _support_ids(active, query, expansions))
    return [item[2] for item in cut_tail(scored)]


def order_ready(records, vector, query="", expansions=()):
    if not (query or expansions):
        return [item[2] for item in cut_tail(vector_order(records, vector))]
    return fuse_order(records, vector, query, expansions)


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
