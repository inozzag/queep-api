"""End-to-end AI checks with synthetic content and disposable development accounts."""
import argparse
import json
import plistlib
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

BASE = "https://queep-api-721281571441.southamerica-east1.run.app"
WORKER = "https://queep-worker-721281571441.southamerica-east1.run.app"
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true")
    local = parser.parse_args().local
    config = plistlib.loads((ROOT.parent / "queep-apple/Config/Firebase/Development/iOS/"
                            "GoogleService-Info.plist").read_bytes())
    assert config["PROJECT_ID"] == "queep-dev-hstlx"
    base = "http://127.0.0.1:8000" if local else BASE
    auth_base = ("http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:"
                 if local else "https://identitytoolkit.googleapis.com/v1/accounts:")
    key = "local" if local else config["API_KEY"]
    client = httpx.Client(timeout=100)
    users, refs, checks = [], [], []

    def auth(action, body):
        response = client.post(f"{auth_base}{action}?key={key}", json=body)
        if response.status_code != 200:
            raise AssertionError(f"Auth {action}: HTTP {response.status_code}")
        return response.json()

    def api(method, path, body=None, token=None, expected=200, data=None):
        token = token or users[0]["idToken"]
        response = client.request(method, base + path, json=body, content=data,
                                  headers={"Authorization": f"Bearer {token}"})
        if response.status_code != expected:
            raise AssertionError(f"{method} {path.split('?')[0]}: HTTP {response.status_code}")
        return response.json() if response.content else None

    def save(title, text, kind="note", url=None):
        identifier = str(uuid4())
        ref = api("PUT", f"/v1/references/{identifier}",
                  {"kind": kind, "title": title, "text": text, "url": url})
        refs.append(ref)
        return identifier

    try:
        if not local:
            assert client.post(WORKER + "/tasks/dispatch").status_code in (401, 403)
        assert client.post(base + "/tasks/dispatch").status_code == 404
        for _ in range(2):
            users.append(auth("signUp", {"email": f"ai-smoke-{uuid4()}@example.invalid",
                                         "password": secrets.token_urlsafe(24),
                                         "returnSecureToken": True}))
        crm = save("Nimbo", "Nimbo é um CRM para times comerciais. Interface clara, fundo branco, "
                   "pipeline de vendas em cartões arrastáveis e cor de destaque azul. "
                   "Não gera vídeos nem imagens.")
        shoes = save("Court Clay", "Tênis para jogar tênis em quadras de saibro. Solado em espinha "
                     "de peixe, cabedal vermelho e reforço lateral. Não é calçado de corrida.")
        laptop = save("Laptop de trabalho", "Notebook de 14 polegadas com 32 GB de RAM e SSD "
                      "de 1 TB para programar e editar vídeos. Sem tela sensível ao toque.")
        history = save("História de uma startup", "Uma entrevista sobre a trajetória da startup "
                       "Aurora: começou com software para escolas e mudou para gestão de clínicas. "
                       "A fundadora conta como testou a ideia entrevistando clientes.")
        repo = save("FrameKit", "Repositório de componentes SwiftUI para construir interfaces "
                    "nativas no iPhone e Mac. Inclui exemplos de navegação e formulários.",
                    kind="link", url="https://github.com/example/framekit")
        image_id = str(uuid4())
        refs.append(api("PUT", f"/v1/references/{image_id}/image?title=Screenshot",
                        data=(ROOT / "evaluation/fixtures/001.png").read_bytes()))
        started = time.monotonic()
        if local:
            sys.path.insert(0, str(ROOT))
            from queep.config import Settings, database, image_bucket
            from queep.images import Images
            from queep.jobs import Jobs
            from queep.worker import engine, process
            assert Settings().environment == "local"
            jobs = Jobs(database())
            for ref in refs:
                task = jobs.payload(jobs.repo.job(users[0]["localId"], ref["id"]).get())
                assert process(task, jobs, engine(), Images(database(), image_bucket())) == "ready"
        pending = {ref["id"] for ref in refs}
        while pending and time.monotonic() - started < 300:
            for identifier in list(pending):
                ref = api("GET", f"/v1/references/{identifier}")
                if ref["status"] == "error":
                    raise AssertionError(f"Analysis failed: {identifier}")
                if ref["status"] == "ready":
                    pending.remove(identifier)
                    assert ref["analysis"]["summary"]
                    if identifier == repo:
                        assert ref["analysis"]["coverage"] == "partial"
                        assert ref["analysis"]["limitations"]
            if pending:
                print(f"Waiting for {len(pending)} synthetic references", flush=True)
                time.sleep(5)
        assert not pending, "Asynchronous processing timed out"
        elapsed = round(time.monotonic() - started, 2)
        print(f"PASS: 6 references processed in {elapsed}s", flush=True)
        queries = [
            ("Interfaces claras de CRM para vendas", {crm, image_id}),
            ("Calçados para jogar em quadra de saibro", {shoes}),
            ("Laptop with 32 GB RAM for programming", {laptop}),
            ("História de uma startup que mudou de segmento", {history}),
            ("Repositórios de componentes para aplicativos SwiftUI", {repo}),
            ("Ferramentas de IA para gerar vídeos", set()),
            ("Receitas de bolo de chocolate", set()),
        ]
        for query, expected in queries:
            started = time.monotonic()
            result = api("POST", "/v1/search", {"query": query})
            found = {ref["id"] for ref in result["items"]}
            checks.append({"query": query, "expected_count": len(expected),
                           "found_count": len(found), "passed": found == expected,
                           "seconds": round(time.monotonic()-started, 2)})
            print(f"Search {len(checks)}: {'PASS' if found == expected else 'FAIL'}", flush=True)
            time.sleep(1.1)
        isolated = api("POST", "/v1/search", {"query": "CRM branco"}, token=users[1]["idToken"])
        assert not isolated["items"] and not isolated["has_pending"]
        api("GET", f"/v1/references/{image_id}", token=users[1]["idToken"], expected=404)
        # Delete after indexing; subsequent search must not resurrect the reference.
        api("DELETE", f"/v1/references/{crm}?revision=1", expected=204)
        result = api("POST", "/v1/search", {"query": "Nimbo CRM branco"})
        assert crm not in {ref["id"] for ref in result["items"]}
        report = {"tested_at": datetime.now(UTC).isoformat(),
                  "execution": "local-worker" if local else "cloud-tasks",
                  "project": "demo-queep" if local else config["PROJECT_ID"],
                  "synthetic_references": len(refs), "processing_seconds": elapsed,
                  "worker_private": None if local else True, "account_isolation": True,
                  "deleted_reference_excluded": True, "queries": checks}
        filename = "local-ai-smoke.json" if local else "cloud-ai-smoke.json"
        (ROOT / "evaluation/results" / filename).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        assert all(check["passed"] for check in checks), "Search quality regression"
        print("PASS: natural-language search, empty matches, partial links, "
              "account isolation, deletion.")
    finally:
        errors = []
        for ref in refs:
            try:
                path = f"/v1/references/{ref['id']}?revision={ref['revision']}"
                api("DELETE", path, expected=204)
            except Exception as error:
                errors.append(str(error))
        for user in users:
            try:
                auth("delete", {"idToken": user["idToken"]})
            except Exception as error:
                errors.append(str(error))
        client.close()
        if errors:
            raise RuntimeError(f"Disposable cleanup needs attention: {errors}")
        print("Removed disposable accounts and reference contents.", flush=True)


if __name__ == "__main__":
    main()
