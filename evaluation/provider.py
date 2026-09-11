"""Small OpenRouter client for a controlled benchmark; never reads the user's library."""

import base64
import hashlib
import json
import math
import time
from pathlib import Path

import httpx
from pydantic import ConfigDict, ValidationError

from queep.models import Interpretation

ROOT = Path(__file__).resolve().parent
PROMPT = (ROOT / "prompt.txt").read_text()
PROMPT_HASH = hashlib.sha256(PROMPT.encode()).hexdigest()


class EvaluationError(Exception):
    """A safe error message, with no credential or provider response body."""


class BudgetExhausted(EvaluationError):
    pass


class Output(Interpretation):
    model_config = ConfigDict(extra="forbid", strict=True)


def schema():
    value = Output.model_json_schema()
    value["required"] = list(value["properties"])
    for item in value["properties"].values():
        item.pop("default", None)
    return value


def routing(model):
    return {
        "only": [model["provider"]],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "max_price": {
            "prompt": model["prompt_per_million"],
            "completion": model["completion_per_million"],
            "request": 0,
        },
    }


def source_parts(reference):
    # Deliberately exclude IDs, relevance labels, queries and review expectations.
    material = {key: reference.get(key, "") for key in ("title", "text", "url")}
    material["source_coverage"] = reference.get("source_coverage", "full")
    parts = [{"type": "text", "text": json.dumps(material, ensure_ascii=False)}]
    if reference.get("image"):
        path = (ROOT / reference["image"]).resolve()
        if not path.is_relative_to(ROOT / "fixtures") or path.suffix != ".png":
            raise EvaluationError("Somente PNGs sintéticos do diretório fixtures são aceitos.")
        content = path.read_bytes()
        if len(content) > 1_000_000:
            raise EvaluationError("Imagem do benchmark excede 1 MB.")
        parts.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(content).decode(),
        }})
    return parts


class Router:
    def __init__(self, client, journal: Path, budget: float, allowance: float):
        self.client = client
        self.journal = journal
        self.budget = budget
        self.allowance = allowance
        self.reserved = 0.0
        self.calls = []

    def request(self, endpoint, payload, label):
        # Reserve BEFORE sending, even on timeout/invalid responses. No automatic retries.
        # This conservative local allowance complements the provider's API-key spending cap;
        # it is not a tokenizer-based guarantee of the invoice amount.
        if self.reserved + self.allowance > self.budget + 1e-9:
            raise BudgetExhausted("Limite local de chamadas atingido; execução interrompida.")
        self.reserved += self.allowance
        entry = {"label": label, "model": payload["model"], "endpoint": endpoint,
                 "reserved_usd": self.allowance, "status": "in_flight"}
        self.calls.append(entry)
        self.flush()
        start = time.monotonic()
        try:
            response = self.client.post(endpoint, json=payload)
            entry["http_status"] = response.status_code
            if response.is_error:
                raise EvaluationError(f"OpenRouter HTTP {response.status_code}.")
            result = response.json()
            if not isinstance(result, dict) or result.get("error"):
                raise EvaluationError("OpenRouter devolveu um erro ou resposta inválida.")
            entry.update({"id": result.get("id"), "actual_model": result.get("model"),
                          "provider": result.get("provider"), "usage": result.get("usage", {})})
            cost = entry["usage"].get("cost")
            if cost is not None:
                cost = float(cost)
                if not math.isfinite(cost) or cost < 0:
                    raise EvaluationError("Custo retornado inválido.")
                entry["cost_usd"] = cost
                if cost > self.allowance:
                    self.reserved = self.budget
                    raise BudgetExhausted("Custo da chamada excedeu a reserva; revisar orçamento.")
            entry["status"] = "received"
            return result
        except httpx.HTTPError:
            entry["status"] = "transport_error"
            raise EvaluationError("Falha de rede; reserva mantida, sem repetição.") from None
        except (ValueError, TypeError, AttributeError):
            entry["status"] = "invalid_response"
            raise EvaluationError("Resposta inválida do OpenRouter.") from None
        except EvaluationError:
            entry["status"] = "provider_error"
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - start, 3)
            self.flush()

    def flush(self):
        self.journal.write_text(json.dumps(self.calls, ensure_ascii=False, indent=2))

    def interpret(self, model, reference, max_output):
        payload = {
            "model": model["id"], "provider": routing(model), "stream": False,
            "temperature": 0, "max_tokens": max_output, "reasoning": {"enabled": False},
            "messages": [{"role": "system", "content": PROMPT},
                         {"role": "user", "content": source_parts(reference)}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "reference_interpretation", "strict": True, "schema": schema(),
            }},
        }
        result = self.request("chat/completions", payload, f"interpret:{reference['id']}")
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("Truncated/refused response")
            raw = json.loads(choice["message"]["content"])
            if set(raw) != set(schema()["required"]):
                raise ValueError("Missing or extra fields")
            output = Output.model_validate(raw)
            if reference.get("source_coverage") == "partial":
                if output.coverage != "partial" or not output.limitations:
                    raise ValueError("Partial material must be identified")
        except (KeyError, IndexError, ValueError, TypeError, ValidationError):
            self.calls[-1]["status"] = "invalid_interpretation"
            self.flush()
            raise EvaluationError("Interpretação incompleta, inválida ou sem limitação exigida.") \
                from None
        self.calls[-1]["status"] = "ok"
        self.flush()
        return output.model_dump()

    def embed(self, model, texts, label):
        if not texts or any(len(text.encode()) > 7000 for text in texts):
            raise EvaluationError("Lote vazio ou texto excessivo para o benchmark de embeddings.")
        payload = {"model": model["id"], "provider": routing(model),
                   "input": texts[0] if len(texts) == 1 else texts,
                   "dimensions": model["dimensions"], "encoding_format": "float"}
        result = self.request("embeddings", payload, label)
        try:
            data = sorted(result["data"], key=lambda item: item["index"])
            if [item["index"] for item in data] != list(range(len(texts))):
                raise ValueError("Missing/duplicate indexes")
            vectors = [item["embedding"] for item in data]
            for vector in vectors:
                if len(vector) != model["dimensions"]:
                    raise ValueError("Wrong dimensions")
                if any(type(x) not in (float, int) or not math.isfinite(x) for x in vector):
                    raise ValueError("Invalid vector")
                if sum(x * x for x in vector) == 0:
                    raise ValueError("Zero vector")
            if result["model"] not in model.get("response_ids", [model["id"]]):
                raise ValueError("Unexpected embedding space")
        except (KeyError, ValueError, TypeError):
            self.calls[-1]["status"] = "invalid_embedding"
            self.flush()
            raise EvaluationError("Embedding inválido ou incompatível; comparação interrompida.") \
                from None
        self.calls[-1]["status"] = "ok"
        self.flush()
        return vectors
