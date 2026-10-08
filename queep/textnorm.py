"""Accent-insensitive tokens for keyword search. Light plurals only — not a full stemmer."""

import re
import unicodedata

# Two-letter design terms that must survive the 3-letter floor.
SHORT_TOKENS = frozenset({"ui", "ux", "ai"})
STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "your", "you", "are", "was",
    "were", "have", "has", "not", "its", "into", "sobre", "para", "como", "mais", "mas",
    "uma", "uns", "umas", "que", "nao", "por", "dos", "das", "num", "numa", "seu", "sua",
    "voce", "entre", "depois", "antes", "muito", "com", "sem", "nos", "nas", "pelo",
    "pela", "pelos", "pelas", "este", "esta", "esse", "essa", "isso", "aqui", "quando",
    "onde", "porque", "também", "tambem", "apenas", "somente",
})
_TOKEN = re.compile(r"[a-z0-9]+")


def fold(text):
    normalized = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(char for char in normalized if not unicodedata.combining(char))
    return stripped.casefold()


def stem(token):
    """PT/EN plurals: websites→website, logos→logo, cores→cor, imagens→imagem."""
    if len(token) <= 3:
        return token
    if token.endswith("oes") and len(token) > 5:
        return token[:-3] + "ao"
    if token.endswith("aes") and len(token) > 5:
        return token[:-3] + "ao"
    if token.endswith("ns") and len(token) > 5:
        return token[:-2] + "m"
    if token.endswith("es") and len(token) > 4 and token[-3] in "rzlns":
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss") and len(token) > 4:
        return token[:-1]
    return token


def tokenize(text):
    folded = fold(text)
    tokens = []
    for raw in _TOKEN.findall(folded):
        if raw in STOPWORDS:
            continue
        if len(raw) < 3 and raw not in SHORT_TOKENS:
            continue
        tokens.append(raw)
        stemmed = stem(raw)
        if stemmed != raw and stemmed not in STOPWORDS:
            tokens.append(stemmed)
    return tokens
