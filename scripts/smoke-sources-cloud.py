"""Public source extraction and search in a disposable development account."""

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

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://queep-api-721281571441.southamerica-east1.run.app"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="Use emulators and a local AI API")
    local = parser.parse_args().local
    config = plistlib.loads((ROOT.parent / "queep-apple/Config/Firebase/Development/iOS/"
                            "GoogleService-Info.plist").read_bytes())
    assert config["PROJECT_ID"] == "queep-dev-hstlx"
    base = "http://127.0.0.1:8000" if local else BASE
    auth_base = ("http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:"
                 if local else "https://identitytoolkit.googleapis.com/v1/accounts:")
    key = "local" if local else config["API_KEY"]
    client = httpx.Client(timeout=100)
    user, refs, checks = None, [], []

    def auth(action, body):
        response = client.post(f"{auth_base}{action}?key={key}", json=body)
        assert response.status_code == 200, f"Auth {action}: HTTP {response.status_code}"
        return response.json()

    def api(method, path, body=None, expected=200):
        response = client.request(method, base + path, json=body,
                                  headers={"Authorization": f"Bearer {user['idToken']}"})
        assert response.status_code == expected, f"{method}: HTTP {response.status_code}"
        return response.json() if response.content else None

    try:
        user = auth("signUp", {"email": f"sources-{uuid4()}@example.invalid",
                              "password": secrets.token_urlsafe(24), "returnSecureToken": True})
        cases = [
            ("https://github.com/encode/httpx", "", "read"),
            ("https://www.python.org/about/", "", "read"),
            ("https://github.com/queep-public-test-missing-20260911/repository",
             "Referência de tipografia brutalista", "unavailable"),
        ]
        for url, text, _ in cases:
            ref = api("PUT", f"/v1/references/{uuid4()}",
                      {"kind": "link", "title": "", "text": text, "url": url})
            refs.append(ref)
        if local:
            sys.path.insert(0, str(ROOT))
            from queep.config import Settings, database
            from queep.jobs import Jobs
            from queep.worker import engine, process
            assert Settings().environment == "local"
            jobs = Jobs(database())
            for ref in refs:
                task = jobs.payload(jobs.repo.job(user["localId"], ref["id"]).get())
                assert process(task, jobs, engine(), None) == "ready"
        started = time.monotonic()
        pending, sources = {ref["id"] for ref in refs}, {}
        while pending and time.monotonic() - started < 300:
            for identifier in list(pending):
                ref = api("GET", f"/v1/references/{identifier}")
                assert ref["status"] != "error", "Source processing failed"
                if ref["status"] == "ready":
                    pending.remove(identifier)
                    sources[identifier] = ref["source"]
                    assert ref["analysis"]["coverage"] == "partial"
            if pending:
                time.sleep(5)
        assert not pending, "Processing timeout"
        for ref, (_, _, state) in zip(refs, cases, strict=True):
            source = sources[ref["id"]]
            assert source["state"] == state, (
                f"{ref['url']}: {source['state']} {source['limitations']}")
        assert "async" in sources[refs[0]["id"]]["text"].lower()
        print("PASS: public README, website, unavailable page and coverage", flush=True)
        queries = [
            ("Bibliotecas com clientes HTTP síncronos e assíncronos em Python", {refs[0]["id"]}),
            ("Python HTTP library with HTTP/2 support", {refs[0]["id"]}),
            ("Sobre a linguagem de programação Python", {refs[1]["id"]}),
            ("Referências de tipografia brutalista", {refs[2]["id"]}),
            ("Ferramentas para gerar vídeo com IA", set()),
        ]
        for query, expected in queries:
            result = api("POST", "/v1/search", {"query": query})
            found = {ref["id"] for ref in result["items"]}
            checks.append({"query": query, "expected_count": len(expected),
                           "found_count": len(found), "passed": expected == found})
            print(f"Search {len(checks)}: {'PASS' if expected == found else 'FAIL'}", flush=True)
            time.sleep(1.1)
        report = {"tested_at": datetime.now(UTC).isoformat(),
                  "execution": "local-worker" if local else "cloud-tasks",
                  "project": "demo-queep" if local else config["PROJECT_ID"],
                  "sources": [{"url": cases[i][0], "state": sources[ref["id"]]["state"],
                               "text_bytes": len(sources[ref["id"]]["text"].encode())}
                              for i, ref in enumerate(refs)], "queries": checks}
        filename = "local-sources-smoke.json" if local else "cloud-sources-smoke.json"
        (ROOT / "evaluation/results" / filename).write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        assert all(check["passed"] for check in checks), "Search quality failure"
    finally:
        errors = []
        for ref in refs:
            try:
                api("DELETE", f"/v1/references/{ref['id']}?revision=1", expected=204)
            except Exception as error:
                errors.append(str(error))
        if user:
            try:
                auth("delete", {"idToken": user["idToken"]})
            except Exception as error:
                errors.append(str(error))
        client.close()
        if errors:
            raise RuntimeError(f"Disposable cleanup needs attention: {errors}")
        print("Removed disposable account and source contents.", flush=True)


if __name__ == "__main__":
    main()
