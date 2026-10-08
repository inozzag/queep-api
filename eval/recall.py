"""Recall of the owner's labeled queries, fusion versus cosine.

The JSON file stores reference ids only. This script does not debit a monthly
search: it embeds each query once and ranks in process. A dead checker, when
``--checker`` is on, keeps the ranked list.

Run where ADC can read ``queep-dev-hstlx`` (this repo has no credentials):

    QUEEP_ENV=production
    GOOGLE_CLOUD_PROJECT=queep-dev-hstlx
    QUEEP_STORAGE_BUCKET=<bucket do projeto dev>
    QUEEP_AI_ENABLED=true
    OPENROUTER_API_KEY=<secret>
    # sem variáveis de emulador

    uv run --env-file .env.evaluation python eval/recall.py --uid UID

About US$0.0002 per query for the embedding. ``--checker`` adds one verifier
call per query and gives up after 20 seconds; a timeout keeps the ranked list.
Empty labels print the count only. Color queries with no evidence are capped
at a short near-miss list. Short themes (1–2 words, no color) keep the fused
head, then only cosine neighbors above a floor, capped so a flat shelf is not
returned whole. A theme with no lexical support stays a short list. After a
real run, tune the constants in ``queep/ranking.py``.
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from queep.ai import AIError  # noqa: E402
from queep.lexicon import expand_terms  # noqa: E402
from queep.models import Reference  # noqa: E402
from queep.ranking import (  # noqa: E402
    BROAD_COSINE_FLOOR,
    BROAD_COSINE_RELATIVE,
    BROAD_MISS_LIMIT,
    BROAD_RESULT_CAP,
    BROAD_TOKEN_LIMIT,
    EXPANSION_WEIGHT,
    FUSED_PROTECT,
    HEAD_LIMIT,
    KEYWORD_WEIGHT,
    MIN_RESULTS,
    RESULT_LIMIT,
    SPECIFIC_MISS_LIMIT,
    TAIL_RELATIVE_GAP,
    VECTOR_WEIGHT,
    contradicting_ids,
    order_ready,
    vector_order,
)

QUERIES = Path(__file__).with_name("owner_queries.json")
_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def load_queries(path=QUERIES):
    data = json.loads(Path(path).read_text())
    labeled = data["labeled"]
    qualitative = data["qualitative"]
    for item in labeled:
        if set(item) != {"query", "relevant"}:
            raise ValueError("labels store query and reference ids only")
        if not isinstance(item["relevant"], list):
            raise ValueError("relevant")
        for identifier in item["relevant"]:
            if not isinstance(identifier, str) or not _ID.match(identifier):
                raise ValueError("reference id")
    if not all(isinstance(query, str) and query.strip() for query in qualitative):
        raise ValueError("qualitative")
    return labeled, qualitative


def recall_at(ranked, relevant, k):
    """Fraction of the labeled ids present in the first k hits. None if unlabeled."""
    if not relevant:
        return None
    return len(set(ranked[:k]) & set(relevant)) / len(relevant)


def fuse_and_cosine(records, vector, query, expansions=None):
    """Ranked ids. Fusion is what the API returns; cosine is the vector order."""
    terms = expand_terms(query) if expansions is None else expansions
    fused = [item["id"] for item in order_ready(records, vector, query, terms)]
    cosine = [item[1] for item in vector_order(records, vector)]
    return fused, cosine


def apply_checker(ai, query, fused_records):
    """Same head filter as search, without reserving a monthly search.

    A verifier error returns the ranked ids unchanged.
    """
    head, tail = fused_records[:HEAD_LIMIT], fused_records[HEAD_LIMIT:]
    refs = [Reference.model_validate(item) for item in head]
    failed = False
    try:
        selected = ai.verify(query, refs) if refs else []
    except AIError:
        failed = True
        selected = [ref.id for ref in refs]
    kept = set(selected)
    if not failed:
        kept -= set(contradicting_ids(query, [ref for ref in refs if ref.id in kept]))
    ordered = [ref.id for ref in refs if ref.id in kept]
    ordered += [item["id"] for item in tail]
    return ordered, failed


def render_labeled(query, fused, cosine, relevant):
    lines = []
    for name, ranked in (("fusão", fused), ("cosseno", cosine)):
        lines.append(_line(query, name, ranked, relevant))
    return lines


def _line(query, name, ranked, relevant):
    r10 = recall_at(ranked, relevant, 10)
    r30 = recall_at(ranked, relevant, 30)
    if relevant:
        return (f"{query} | {name} | n={len(ranked)} | "
                f"R@10={r10:.3f} | R@30={r30:.3f}")
    return (f"{query} | {name} | n={len(ranked)} | R@10=— | R@30=— | "
            "rótulo vazio; lista curta e não vazia é aceitável")


def render_header():
    return (
        f"constantes: janela={RESULT_LIMIT} mínimo={MIN_RESULTS} "
        f"protege={FUSED_PROTECT} teto_amplo={BROAD_RESULT_CAP} "
        f"piso={BROAD_COSINE_FLOOR} relativo={BROAD_COSINE_RELATIVE} "
        f"sem_apoio={BROAD_MISS_LIMIT} "
        f"palavras_amplas<={BROAD_TOKEN_LIMIT} "
        f"sem_cor={SPECIFIC_MISS_LIMIT} "
        f"cabeça={HEAD_LIMIT} folga={TAIL_RELATIVE_GAP} "
        f"pesos vetor/palavra/expansão="
        f"{VECTOR_WEIGHT}/{KEYWORD_WEIGHT}/{EXPANSION_WEIGHT}"
    )


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True)
    parser.add_argument("--queries", default=str(QUERIES))
    parser.add_argument("--checker", action="store_true",
                        help="Also run the contradiction checker. Extra paid calls.")
    parser.add_argument("--top", type=int, default=10, help="Ids shown for qualitative queries.")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    labeled, qualitative = load_queries(args.queries)
    from google.cloud.firestore_v1.base_query import FieldFilter

    from queep.ai import AI, SPACE
    from queep.budget import Budget
    from queep.config import Settings, database
    from queep.repository import Repository

    settings = Settings()
    if not settings.ai_enabled or not settings.openrouter_key:
        print("Precisa de QUEEP_AI_ENABLED=true e OPENROUTER_API_KEY.", file=sys.stderr)
        return 2
    db = database()
    ai = AI(settings.openrouter_key.get_secret_value(),
            Budget(db, settings.ai_daily_budget, settings.search_daily_limit))
    ready = (Repository(db).collection(args.uid)
             .where(filter=FieldFilter("deleted", "==", False))
             .where(filter=FieldFilter("status", "==", "ready"))
             .where(filter=FieldFilter("embedding_space", "==", SPACE)))
    records = [doc.to_dict() for doc in ready.stream()]
    print(render_header())
    print(f"prontos: {len(records)}")
    if not records:
        print("Nenhum item pronto nesse espaço de embedding.")
        return 0
    for item in labeled:
        vector = ai.embed(item["query"], query=True)
        fused_records = order_ready(records, vector, item["query"], expand_terms(item["query"]))
        fused = [row["id"] for row in fused_records]
        cosine = [row[1] for row in vector_order(records, vector)]
        for line in render_labeled(item["query"], fused, cosine, item["relevant"]):
            print(line)
        if args.checker:
            checked, failed = apply_checker(ai, item["query"], fused_records)
            note = "checker falhou; lista ordenada mantida" if failed else "checker ok"
            print(f"{item['query']} | checker | n={len(checked)} | {note}")
    print("qualitativas (sem rótulo):")
    for query in qualitative:
        vector = ai.embed(query, query=True)
        fused, _cosine = fuse_and_cosine(records, vector, query)
        shown = ", ".join(fused[:args.top]) or "—"
        print(f"{query} | n={len(fused)} | topo: {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
