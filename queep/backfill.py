"""Re-analyze or re-embed saved references up to interpretation v8.

Dry-run is the default. Nothing is written and no model is called until
``--apply`` or ``--enqueue``. This machine is not expected to hold credentials;
run the command where ADC can see project ``queep-dev-hstlx``.

Items live at ``users/{uid}/references/{id}``. ``--reembed-only`` rebuilds the
vector from fields already stored (appearance and the user note included) and
does not stamp ``analysis_version``, so a later full run still re-reads the item.
``--enqueue`` writes the worker outbox (``jobs`` or ``jobs_testflight`` via
``QUEEP_JOB_COLLECTION``). The deployed worker then re-fetches the page, sends
the preview image, and publishes v8. Local ``--apply`` does not download images.
"""

import argparse
import logging
import sys
import time
from datetime import UTC, datetime

from fastapi import HTTPException
from google.cloud.firestore_v1.vector import Vector

from queep.ai import ANALYSIS_VERSION, INTERPRETER, SPACE, AIError, document
from queep.budget import Budget
from queep.models import Interpretation, Reference
from queep.repository import Repository, new_job

logger = logging.getLogger("queep.backfill")

# Provider estimates, not the US$0.01 reservation. Calibrated from a diagnostic
# call (~US$0.0003) plus one vision interpretation and one embedding.
PROVIDER_REANALYZE_USD = 0.0035
PROVIDER_VIDEO_USD = 0.010
PROVIDER_REEMBED_USD = 0.0002
DEFAULT_MAX_COST_USD = 0.50
DEFAULT_RATE_SECONDS = 1.0
# Marks a vector rebuilt from stored fields. It is not analysis_version.
EMBED_BUILD = "queep-embed-v8-fields"
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "instagram.com")


def video_like(record):
    """Clips that can spend a transcription plus a vision call."""
    if record.get("kind") in {"audio", "video"} or record.get("media") or record.get("transcript"):
        return True
    url = (record.get("url") or "").lower()
    return any(host in url for host in _VIDEO_HOSTS)


def provider_usd(record, reembed_only):
    if reembed_only:
        return PROVIDER_REEMBED_USD
    if video_like(record):
        return PROVIDER_VIDEO_USD
    return PROVIDER_REANALYZE_USD


def reserve_usd(record, reembed_only):
    """Upper bound held by ``ai_budget`` before settlement (US$0.01 per call)."""
    if reembed_only:
        calls = 1
    elif video_like(record):
        calls = 4
    else:
        calls = 2
    return Budget.ALLOWANCE * calls


def _version(record):
    return record.get("analysis_version") or ""


def skip_reason(record, *, force, reembed_only):
    """Why this row is left alone. ``None`` means it is eligible."""
    if record.get("deleted"):
        return "deleted"
    status = record.get("status")
    if status in {"queued", "processing"}:
        return "in-flight"
    if status not in {"ready", "error"}:
        return "not-saved"
    if reembed_only:
        if not record.get("analysis"):
            return "no-analysis"
        if status != "ready":
            return "not-ready"
        if not force and (_version(record) == ANALYSIS_VERSION
                          or record.get("embedding_build") == EMBED_BUILD):
            return "current"
        return None
    if not force and _version(record) == ANALYSIS_VERSION:
        return "current"
    return None


class Backfill:
    def __init__(self, db, ai=None, *, sleep=time.sleep, now=None, rate=DEFAULT_RATE_SECONDS,
                 daily_limit=1.0):
        self.db = db
        self.ai = ai
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(UTC))
        self.rate = rate
        self.daily_limit = daily_limit
        self.repo = Repository(db)

    def _users(self, uid):
        if uid:
            return [uid]
        return [doc.id for doc in self.db.collection("users").stream()]

    def _rows(self, uid):
        rows = []
        for owner in self._users(uid):
            collection = self.db.collection("users").document(owner).collection("references")
            for doc in collection.stream():
                data = doc.to_dict() or {}
                data["id"] = data.get("id") or doc.id
                rows.append((owner, data))
        rows.sort(key=lambda item: (item[0], item[1].get("id") or ""))
        return rows

    def _daily_used(self):
        day = self.now().astimezone(UTC).date().isoformat()
        data = self.db.collection("ai_budget").document(day).get().to_dict() or {}
        return float(data.get("used") or 0.0)

    def run(self, *, uid=None, limit=None, force=False, reembed_only=False,
            max_cost_usd=DEFAULT_MAX_COST_USD, mode="dry-run", action=None):
        if mode not in {"dry-run", "apply", "enqueue"}:
            raise ValueError("mode")
        action = action or ("reembed" if reembed_only else "reanalyze")
        if action not in {"reanalyze", "reembed", "enqueue"}:
            raise ValueError("action")
        reembed_only = action == "reembed"
        if reembed_only and mode == "enqueue":
            raise ValueError("reembed-only does not enqueue; the worker always re-analyzes")
        skipped = {}
        planned = []
        for owner, record in self._rows(uid):
            reason = skip_reason(record, force=force, reembed_only=reembed_only)
            if reason:
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            planned.append({
                "uid": owner,
                "id": record["id"],
                "action": action,
                "analysis_version": _version(record),
                "provider_usd": provider_usd(record, reembed_only),
                "reserve_usd": reserve_usd(record, reembed_only),
                "record": record,
            })
            if limit is not None and len(planned) >= limit:
                break
        remaining = max(0.0, self.daily_limit - self._daily_used())
        selected, spent, reserved, stopped = _fit(planned, max_cost_usd, remaining)
        completed, failed = [], []
        if mode != "dry-run":
            for item in selected:
                outcome = self._execute(item, reembed_only=reembed_only, mode=mode)
                if outcome == "budget":
                    stopped = "budget"
                    break
                if outcome == "ok":
                    completed.append(item["id"])
                else:
                    failed.append({"id": item["id"], "error": outcome})
                if self.rate > 0:
                    self.sleep(self.rate)
        return {
            "mode": mode,
            "job_collection": self.repo.job_collection,
            "planned": [_public(item) for item in planned],
            "selected": [_public(item) for item in selected],
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "catalog_usd": round(sum(item["provider_usd"] for item in planned), 6),
            "estimated_usd": round(spent, 6),
            "reserved_usd": round(reserved, 6),
            "stopped": stopped,
            "daily_remaining_usd": round(remaining, 6),
            "max_cost_usd": max_cost_usd,
        }

    def _execute(self, item, *, reembed_only, mode):
        record = item["record"]
        try:
            if mode == "enqueue":
                self._enqueue(item["uid"], record)
                logger.info("backfill uid=%s id=%s action=enqueue result=ok",
                            item["uid"], item["id"])
                return "ok"
            if self.ai is None:
                raise RuntimeError("apply requires an AI client")
            reference = Reference.model_validate(record)
            if reembed_only:
                analysis = Interpretation.model_validate(record["analysis"])
            else:
                analysis = self.ai.interpret(reference)
            vector = self.ai.embed(document(reference, analysis))
            self._write(item["uid"], record, analysis, vector, reembed_only=reembed_only)
        except HTTPException as exc:
            if exc.status_code == 429:
                logger.warning("backfill uid=%s id=%s action=%s result=budget",
                               item["uid"], item["id"], item["action"])
                return "budget"
            logger.warning("backfill uid=%s id=%s action=%s result=%s",
                           item["uid"], item["id"], item["action"], exc.status_code)
            return f"http_{exc.status_code}"
        except (AIError, ValueError, RuntimeError) as exc:
            logger.warning("backfill uid=%s id=%s action=%s result=%s",
                           item["uid"], item["id"], item["action"], exc)
            return str(exc) or exc.__class__.__name__
        logger.info("backfill uid=%s id=%s action=%s result=ok",
                    item["uid"], item["id"], item["action"])
        return "ok"

    def _write(self, uid, record, analysis, vector, *, reembed_only):
        now = self.now()
        payload = {
            "embedding": Vector(vector),
            "embedding_space": SPACE,
            "embedding_build": EMBED_BUILD,
            "updated_at": now,
        }
        if not reembed_only:
            payload.update({
                "status": "ready",
                "analysis": analysis.model_dump(),
                "analysis_model": INTERPRETER["id"],
                "analysis_version": ANALYSIS_VERSION,
            })
        self.repo.collection(uid).document(record["id"]).update(payload)

    def _enqueue(self, uid, record):
        now = self.now()
        job = self.repo.job(uid, record["id"])
        existing = job.get()
        if existing.exists:
            state = (existing.to_dict() or {}).get("state")
            if state in {"pending", "processing"}:
                raise ValueError("already-queued")
        self.repo.collection(uid).document(record["id"]).update(
            {"status": "queued", "updated_at": now})
        job.set(new_job(uid, record["id"], record["revision"], now) | {
            "plan": "backfill", "reason": ANALYSIS_VERSION})


def _fit(planned, max_cost, remaining):
    selected = []
    spent = reserved = 0.0
    stopped = None
    for item in planned:
        if spent + item["provider_usd"] > max_cost + 1e-9:
            stopped = stopped or "cap"
            break
        if reserved + item["reserve_usd"] > remaining + 1e-9:
            stopped = stopped or "budget"
            break
        selected.append(item)
        spent += item["provider_usd"]
        reserved += item["reserve_usd"]
    return selected, spent, reserved, stopped


def _public(item):
    return {key: item[key] for key in
            ("uid", "id", "action", "analysis_version", "provider_usd", "reserve_usd")}


def format_report(result):
    lines = [
        f"modo: {result['mode']}",
        f"coleção de jobs: {result['job_collection']}",
        f"elegíveis: {len(result['planned'])}",
        f"dentro do teto: {len(result['selected'])}",
        f"custo estimado (provedor): US$ {result['estimated_usd']:.4f}",
        f"catálogo inteiro: US$ {result['catalog_usd']:.4f}",
        f"reserva máxima no ai_budget: US$ {result['reserved_usd']:.4f}",
        f"teto --max-cost-usd: US$ {result['max_cost_usd']:.4f}",
        f"orçamento do dia ainda livre: US$ {result['daily_remaining_usd']:.4f}",
        f"parou: {result['stopped'] or 'não'}",
        f"concluídos: {len(result['completed'])}",
        f"falhas: {len(result['failed'])}",
        "ignorados: " + (", ".join(f"{name}={count}" for name, count in
                                    sorted(result["skipped"].items())) or "0"),
    ]
    for item in result["selected"]:
        lines.append(
            f"- {item['uid']} {item['id']} {item['action']} "
            f"versão={item['analysis_version'] or 'nenhuma'} "
            f"US$ {item['provider_usd']:.4f}")
    for item in result["failed"]:
        lines.append(f"! {item['id']} {item['error']}")
    return "\n".join(lines)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", help="Uma conta. Sem isto, percorre users/*.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true",
                        help="Inclui itens que já estão na v8 ou já foram re-embutidos.")
    parser.add_argument("--reembed-only", action="store_true",
                        help="Só reconstrói o vetor. Não chama o modelo de leitura.")
    parser.add_argument("--apply", action="store_true",
                        help="Executa neste processo. O padrão é só imprimir o plano.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Imprime o plano mesmo se --apply ou --enqueue estiver presente.")
    parser.add_argument("--enqueue", action="store_true",
                        help="Grava jobs no outbox para o worker publicado reprocessar.")
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--daily-budget-usd", type=float, default=None,
                        help="Teto do ai_budget. Padrão: QUEEP_AI_DAILY_BUDGET_USD.")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE_SECONDS,
                        help="Segundos entre itens quando executa.")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")
    if args.max_cost_usd < 0:
        parser.error("--max-cost-usd must be >= 0")
    if args.reembed_only and args.enqueue:
        parser.error("--reembed-only cannot be combined with --enqueue")
    return args


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)
    from queep.ai import AI
    from queep.config import Settings, database

    settings = Settings()
    db = database()
    if args.reembed_only:
        action = "reembed"
    elif args.enqueue:
        action = "enqueue"
    else:
        action = "reanalyze"
    if args.dry_run or not (args.apply or args.enqueue):
        mode = "dry-run"
    elif args.enqueue:
        mode = "enqueue"
    else:
        mode = "apply"
    ai = None
    if mode == "apply":
        if not settings.ai_enabled or not settings.openrouter_key:
            print("Apply precisa de QUEEP_AI_ENABLED=true e OPENROUTER_API_KEY.", file=sys.stderr)
            return 2
        ai = AI(settings.openrouter_key.get_secret_value(),
                Budget(db, settings.ai_daily_budget, settings.search_daily_limit))
    daily = settings.ai_daily_budget if args.daily_budget_usd is None else args.daily_budget_usd
    result = Backfill(db, ai, rate=args.rate, daily_limit=daily).run(
        uid=args.uid, limit=args.limit, force=args.force, reembed_only=args.reembed_only,
        max_cost_usd=args.max_cost_usd, mode=mode, action=action)
    print(format_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
