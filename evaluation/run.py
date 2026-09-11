"""Run with `uv run --env-file .env.evaluation python -m evaluation.run --live`."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime

import httpx

from evaluation.provider import PROMPT_HASH, ROOT, BudgetExhausted, EvaluationError, Router
from evaluation.scoring import calibrate, evaluate, rank


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False))


def load_corpus():
    corpus = json.loads((ROOT / "corpus.json").read_text())
    if not corpus.get("synthetic_only"):
        raise EvaluationError("Esta ferramenta aceita somente o acervo sintético versionado.")
    ids = [reference["id"] for reference in corpus["references"]]
    queries = corpus["queries"]
    if len(set(ids)) != len(ids) or len({q["id"] for q in queries}) != len(queries):
        raise EvaluationError("IDs duplicados no acervo.")
    for query in queries:
        if query["split"] not in ("calibration", "test"):
            raise EvaluationError("Divisão inválida do acervo.")
        if query["mode"] not in ("known", "set", "empty"):
            raise EvaluationError("Modo inválido de consulta.")
        if not set(query["expected"]).issubset(ids):
            raise EvaluationError("Consulta menciona referência inexistente.")
        if bool(query["expected"]) == (query["mode"] == "empty"):
            raise EvaluationError("Consulta sem resposta deve usar modo empty.")
    return corpus


def document(reference, analysis=None):
    original = "\n".join(reference.get(key, "") for key in ("title", "text", "url"))
    return "Represent this document for retrieval: " + original + (
        "\nInterpretação:\n" + json.dumps(analysis, ensure_ascii=False)
        if analysis else "\n" + ("Imagem sem legenda." if reference.get("image") else "")
    )


def embed_all(router, model, texts, prefix):
    vectors = []
    # Vertex through OpenRouter currently routes single inputs, while batched inputs
    # route only to AI Studio, which did not satisfy ZDR in our integration check.
    for offset, text in enumerate(texts):
        vectors.extend(router.embed(model, [text], f"{prefix}:{offset}"))
    return vectors


def report_scores(corpus, vectors, query_vectors):
    queries = corpus["queries"]
    rankings = {query["id"]: rank(vectors, vector)
                for query, vector in zip(queries, query_vectors, strict=True)}
    calibration = [q for q in queries if q["split"] == "calibration"]
    heldout = [q for q in queries if q["split"] == "test"]
    threshold = calibrate(calibration, rankings)
    return {"threshold_from_calibration": threshold,
            "calibration": evaluate(calibration, rankings, threshold),
            "test": evaluate(heldout, rankings, threshold),
            "by_format": {kind: evaluate([q for q in heldout if q["format"] == kind],
                                         rankings, threshold)
                          for kind in sorted({q["format"] for q in heldout})},
            "rankings": rankings}


def cost_summary(calls):
    known = [call["cost_usd"] for call in calls if "cost_usd" in call]
    return {"reported_cost_usd": sum(known), "calls_without_reported_cost": len(calls)-len(known),
            "reserved_usd": sum(call["reserved_usd"] for call in calls), "calls": len(calls),
            "sum_call_seconds": sum(call["seconds"] for call in calls)}


def markdown(report):
    def percent(value):
        return f"{100*value:.1f}%" if value is not None else "—"

    lines = ["# Queep — comparação inicial de modelos", "", f"Estado: **{report['status']}**.",
             "Acervo sintético pequeno; não certifica fontes reais, áudio/vídeo ou produção.", "",
             "| Modelo | Análises válidas | Top 5 (teste) | Precisão conjuntos | Recall | "
             "Falsos positivos vazios | Custo reportado USD |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for model, result in report.get("results", {}).items():
        available = model == "raw_text" or bool(result.get("valid_interpretations"))
        score = result.get("scores", {}).get("test", {}) if available else {}
        cost = result["cost"]
        valid = result.get("valid_interpretations")
        count = str(valid) if valid is not None else "—"
        if not available:
            count = "Indisponível"
        cost_text = f"{cost['reported_cost_usd']:.6f}"
        if cost["calls_without_reported_cost"]:
            cost_text += f" + {cost['calls_without_reported_cost']} chamadas sem custo informado"
        lines.append(f"| {model} | {count} | "
                     f"{percent(score.get('known_hit_at_5'))} | "
                     f"{percent(score.get('set_precision'))} | "
                     f"{percent(score.get('set_recall'))} | "
                     f"{percent(score.get('empty_false_positive_rate'))} | "
                     f"{cost_text} |")
    lines.extend(["", "Custo de cada modelo inclui interpretação e vetorização de documentos. "
                  "Embeddings das consultas são compartilhados e contabilizados à parte. "
                  "Taxas de compra de créditos, Firebase, tráfego e extração não estão incluídos.",
                  "", "`raw_text` usa apenas texto recebido; imagens sem legenda ficam sem "
                  "descrição. Comparar esse baseline por formato, não como um modelo multimodal.",
                  "", "Análises que falham continuam no denominador: seus itens são omitidos "
                  "da busca, sem reduzir os resultados esperados. O limiar é escolhido em "
                  "consultas de calibração e aplicado sem ajuste às consultas de teste.",
                  "O top 5 considera apenas resultados exibidos após o limiar. A posição "
                  "sem esse filtro fica separada no JSON (`raw_rank_hit_at_5`). "
                  "Modelos sem rota disponível não recebem nota de qualidade.",
                  "", "Revisar também as interpretações: métricas de busca não detectam "
                  "todos os fatos inventados."])
    if report.get("error"):
        lines.extend(["", "Interrupção: " + report["error"]])
    return "\n".join(lines) + "\n"


def execute(router, config, corpus, output, report):
    refs = corpus["references"]
    query_texts = ["Represent this question for retrieving supporting documents: " + q["query"]
                   for q in corpus["queries"]]
    start = len(router.calls)
    query_vectors = embed_all(router, config["embedding"], query_texts, "queries")
    report["query_embedding_cost"] = cost_summary(router.calls[start:])
    save(output / "query-vectors.json", query_vectors)
    candidates = [None, *config["interpreters"]]
    for candidate in candidates:
        name = candidate["id"] if candidate else "raw_text"
        print(f"Avaliando {name}...", flush=True)
        first_call = len(router.calls)
        analyses, errors = {}, {}
        analyses_path = output / (name.replace("/", "_") + "-analyses.json")
        if candidate:
            for ref in refs:
                try:
                    analyses[ref["id"]] = router.interpret(candidate, ref,
                                                          config["max_output_tokens"])
                except BudgetExhausted:
                    raise
                except EvaluationError as error:
                    errors[ref["id"]] = str(error)
                    # Authentication, privacy routing or schema rejection can affect every
                    # item. Stop a candidate after three failures rather than waste calls.
                    if len(errors) == 3 and not analyses:
                        errors["remaining"] = "Modelo indisponível; itens restantes não enviados."
                        break
                finally:
                    save(analyses_path, analyses)
                    save(output / (name.replace("/", "_") + "-errors.json"), errors)
        selected = [ref for ref in refs if not candidate or ref["id"] in analyses]
        documents = [document(ref, analyses.get(ref["id"])) for ref in selected]
        vectors = embed_all(router, config["embedding"], documents, name) if documents else []
        by_id = dict(zip([ref["id"] for ref in selected], vectors, strict=True))
        scores = report_scores(corpus, by_id, query_vectors)
        cost = cost_summary(router.calls[first_call:])
        valid = len(analyses) if candidate else None
        successes = scores["test"]["successful_known_queries"]
        result = {"valid_interpretations": valid, "total_references": len(refs), "errors": errors,
                  "scores": scores, "cost": cost,
                  "benchmark_cost_per_known_hit_usd": cost["reported_cost_usd"] / successes
                  if successes and not cost["calls_without_reported_cost"] else None}
        report["results"][name] = result
        save(output / (name.replace("/", "_") + "-analyses.json"), analyses)
        save(output / (name.replace("/", "_") + "-vectors.json"), by_id)
        save(output / "report.json", report)
        (output / "report.md").write_text(markdown(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Enviar dados sintéticos ao OpenRouter")
    parser.add_argument("--budget-usd", type=float, default=1.0)
    parser.add_argument("--model", help="Avaliar um único candidato pelo ID exato")
    args = parser.parse_args()
    corpus = load_corpus()
    config = json.loads((ROOT / "models.json").read_text())
    if args.model:
        config["interpreters"] = [m for m in config["interpreters"] if m["id"] == args.model]
        if not config["interpreters"]:
            raise SystemExit("Modelo não pertence à lista aprovada.")
    if not 0 < args.budget_usd <= 1:
        raise SystemExit("Orçamento desta avaliação deve estar entre 0 e US$ 1.")
    plan = {"live": args.live, "references": len(corpus["references"]),
            "queries": len(corpus["queries"]), "models": [m["id"] for m in config["interpreters"]],
            "embedding": config["embedding"]["id"], "budget_usd": args.budget_usd}
    print(json.dumps(plan, ensure_ascii=False, indent=2), flush=True)
    if not args.live:
        print("Prévia apenas: nenhum modelo chamado, nenhum custo ou resultado de qualidade.")
        return
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("Configure OPENROUTER_API_KEY em .env.evaluation.")
    os.umask(0o077)
    output = ROOT / "runs" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir(parents=True, exist_ok=False)
    fixtures = {ref["image"]: hashlib.sha256((ROOT / ref["image"]).read_bytes()).hexdigest()
                for ref in corpus["references"] if ref.get("image")}
    save(output / "manifest.json", {"corpus_sha256": digest(corpus), "config": config,
                                   "prompt_sha256": PROMPT_HASH, "fixtures_sha256": fixtures,
                                   "plan": plan, "synthetic_only": True})
    report = {"status": "running", "results": {}}
    with httpx.Client(base_url="https://openrouter.ai/api/v1/", timeout=60,
                      headers={"Authorization": f"Bearer {key}",
                               "X-OpenRouter-Title": "Queep synthetic evaluation"}) as client:
        router = Router(client, output / "calls.json", args.budget_usd,
                        config["call_allowance_usd"])
        try:
            execute(router, config, corpus, output, report)
            report["status"] = "completed_with_errors" if any(
                result["errors"] for result in report["results"].values()) else "completed"
        except EvaluationError as error:
            report.update(status="interrupted", error=str(error))
        finally:
            report["total_cost"] = cost_summary(router.calls)
            save(output / "report.json", report)
            (output / "report.md").write_text(markdown(report))
    print(f"Relatório: {output / 'report.md'}", flush=True)
    if report["status"] == "interrupted":
        raise SystemExit(report["error"])


if __name__ == "__main__":
    main()
