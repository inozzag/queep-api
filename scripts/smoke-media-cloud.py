"""Exercise cloud media and social metadata with NEW disposable identities only."""

import json
import plistlib
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://queep-api-721281571441.southamerica-east1.run.app"


def main():
    apple = ROOT.parent / "queep-apple"
    config = plistlib.loads((apple / "Config/Firebase/Development/iOS/"
                            "GoogleService-Info.plist").read_bytes())
    assert config["PROJECT_ID"] == "queep-dev-hstlx"
    users, references, checks = [], [], []
    client = httpx.Client(timeout=150)

    def auth(action, body):
        response = client.post("https://identitytoolkit.googleapis.com/v1/accounts:" + action,
                               params={"key": config["API_KEY"]}, json=body)
        assert response.status_code == 200, f"Auth {action}: HTTP {response.status_code}"
        return response.json()

    def api(user, method, path, *, body=None, data=None, expected=200):
        response = client.request(method, BASE + path, json=body, content=data,
                                  headers={"Authorization": "Bearer " + user["idToken"]})
        assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}"
        return response

    try:
        for _ in range(2):
            users.append(auth("signUp", {"email": f"media-smoke-{uuid4()}@example.invalid",
                                         "password": secrets.token_urlsafe(24),
                                         "returnSecureToken": True}))
        owner, stranger = users
        for name in ("sample-audio.wav", "sample-video.mp4"):
            data = (apple / "Tests/Fixtures" / name).read_bytes()
            identifier = str(uuid4())
            path = f"/v1/references/{identifier}/media"
            ref = api(owner, "PUT", path, data=data).json()
            references.append(ref)
            repeated = api(owner, "PUT", path, data=data).json()
            assert repeated["id"] == ref["id"] and repeated["revision"] == 1
            api(owner, "PUT", path + "?title=conflict", data=data, expected=409)
            stored = api(owner, "GET", path)
            assert stored.headers["cache-control"] == "no-store" and len(stored.content) > 1000
            (ROOT / ".local").mkdir(exist_ok=True)
            (ROOT / f".local/media-smoke-{ref['kind']}.mp4").write_bytes(stored.content)
            api(stranger, "GET", path, expected=404)
            api(owner, "GET", path + "?thumbnail=true",
                expected=200 if ref["kind"] == "video" else 404)
        audio, video = references
        print("PASS: cloud upload, immutable retry, private playback and isolation", flush=True)
        sources = [
            "https://www.youtube.com/watch?v=M7lc1UVf-VE",
            "https://vimeo.com/76979871",
            "https://www.instagram.com/p/queep-unavailable-fixture/",
        ]
        for url in sources:
            ref = api(owner, "PUT", f"/v1/references/{uuid4()}",
                      body={"kind": "link", "url": url, "text": "", "title": ""}).json()
            references.append(ref)
        ready, started = {}, time.monotonic()
        while len(ready) < len(references) and time.monotonic() - started < 600:
            for original in references:
                if original["id"] in ready:
                    continue
                ref = api(owner, "GET", f"/v1/references/{original['id']}").json()
                assert ref["status"] != "error", f"Processing failed for {ref['kind']}"
                if ref["status"] == "ready":
                    ready[ref["id"]] = ref
                    print(f"Ready: {ref['kind']}", flush=True)
            if len(ready) < len(references):
                time.sleep(5)
        assert len(ready) == len(references), "Cloud processing timed out"
        # Synthetic inputs only. Preserve evidence even if a quality assertion fails.
        (ROOT / ".local/media-smoke-analysis.json").write_text(
            json.dumps(list(ready.values()), ensure_ascii=False, indent=2) + "\n")
        for ref in (audio, video):
            result = ready[ref["id"]]
            assert "aurora" in result["transcript"].lower()
        assert not ready[audio["id"]]["analysis"]["appearance"]
        visual = ready[video["id"]]["analysis"]
        assert visual["coverage"] == "partial"
        assert not any(word in " ".join(visual["appearance"]).lower()
                       for word in ("botões", "tabela", "interface", "tipografia"))
        for ref in references[2:]:
            source = ready[ref["id"]]["source"]
            assert source["limitations"]
            assert ready[ref["id"]]["analysis"]["coverage"] == "partial"
            assert source["state"] == "unavailable" or source["content_kind"] == "metadata"
        assert ready[references[2]["id"]]["source"]["state"] == "read", "YouTube metadata"
        print("PASS: transcription, sampled visuals and social-link coverage", flush=True)
        queries = [
            ("Ferramenta de IA que organiza clipes de jogos e sugere títulos de vídeos",
             {audio["id"], video["id"]}),
            ("Vídeos que mostram uma interface com botões brancos e tabela de partidas", set()),
            ("Vídeos sobre a ferramenta Aurora", {video["id"]}),
            ("Áudios sobre ferramentas de IA", {audio["id"]}),
            ("Receitas de bolo de chocolate", set()),
        ]
        for query, expected in queries:
            found = {ref["id"] for ref in api(owner, "POST", "/v1/search",
                                              body={"query": query}).json()["items"]}
            checks.append({"query": query, "expected_count": len(expected),
                           "found_count": len(found), "passed": found == expected})
            print(f"Search {len(checks)}: {'PASS' if found == expected else 'FAIL'}", flush=True)
            time.sleep(1.1)
        report = {"tested_at": datetime.now(UTC).isoformat(), "project": config["PROJECT_ID"],
                  "processing_and_search_seconds": round(time.monotonic() - started, 1),
                  "media": [{"kind": ref["kind"], "duration": ref["media"]["duration_seconds"],
                             "summary": ready[ref["id"]]["analysis"]["summary"],
                             "appearance": ready[ref["id"]]["analysis"]["appearance"]}
                            for ref in (audio, video)],
                  "sources": [{"url": ref["url"], "state": ready[ref["id"]]["source"]["state"]}
                              for ref in references[2:]], "queries": checks}
        (ROOT / "evaluation/results/cloud-media-smoke.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        assert all(check["passed"] for check in checks), "Search quality failure"
    finally:
        errors = []
        if users:
            for ref in references:
                try:
                    api(users[0], "DELETE", f"/v1/references/{ref['id']}?revision=1", expected=204)
                    if ref.get("media"):
                        api(users[0], "GET", f"/v1/references/{ref['id']}/media", expected=404)
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
        print("Removed synthetic references and accounts.", flush=True)


if __name__ == "__main__":
    main()
