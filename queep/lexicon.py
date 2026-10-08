"""Small PT/EN design vocabulary. Expansion boosts rank and never filters."""

from queep.ranking import COLOR_FAMILIES
from queep.textnorm import fold, tokenize

# Each tuple is one synonym group. Multi-word members match as phrases.
_GROUPS = (
    ("web design", "site", "website", "landing page", "página", "hero", "ui"),
    ("interface", "ui", "tela", "screen", "dashboard", "layout"),
    ("mobile", "app", "aplicativo", "celular", "iphone", "ios", "android"),
    ("paywall", "assinatura", "planos", "pricing", "upgrade"),
    ("login", "entrar", "sign in", "cadastro"),
    ("logo", "logotipo", "marca", "ícone", "icone"),
    ("hero", "topo", "banner", "header"),
    ("e-commerce", "loja", "checkout", "produto"),
    ("onboarding", "boas-vindas", "primeiro acesso"),
    ("tipografia", "type", "font", "fonte"),
)


def _clusters():
    groups = list(_GROUPS)
    for names in COLOR_FAMILIES.values():
        groups.append(tuple(names))
    return groups


def _hits(member, folded, tokens):
    folded_member = fold(member)
    if " " in folded_member or "-" in folded_member:
        return folded_member in folded
    return folded_member in tokens


def expand_terms(query):
    """Terms from every cluster the query touches, excluding words already present."""
    folded = fold(query)
    tokens = set(tokenize(query))
    extra = []
    seen = set()
    for cluster in _clusters():
        if not any(_hits(member, folded, tokens) for member in cluster):
            continue
        for member in cluster:
            key = fold(member)
            if _hits(member, folded, tokens) or key in seen:
                continue
            seen.add(key)
            extra.append(member)
    return extra
