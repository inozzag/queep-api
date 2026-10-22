"""Fetch reprocess selects the incomplete cohorts and refuses a foreign version."""

from datetime import UTC, datetime
from uuid import uuid4

from queep.ai import ANALYSIS_VERSION
from queep.config import database
from queep.models import Capture
from queep.repository import Repository
from queep.reprocess import main


def _ready(repo, uid, url, **fields):
    ref = repo.save(uid, str(uuid4()), Capture(kind="link", url=url))
    repo.collection(uid).document(ref.id).update({"status": "ready", **fields})
    return ref.id


def test_enqueue_refuses_a_version_the_worker_is_not_serving(capsys):
    code = main([
        "--enqueue", "--require-version", "queep-interpretation-v7-portrait", "--uid", "nobody",
    ])
    assert code == 2
    err = capsys.readouterr().err
    assert ANALYSIS_VERSION in err
    assert "queep-interpretation-v7-portrait" not in err.split("must equal", 1)[-1]


def test_dry_run_orders_cohorts_and_skips_dribbble(capsys):
    repo = Repository(database())
    uid = uuid4().hex
    now = datetime.now(UTC)
    error = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://example.com/broken"))
    repo.collection(uid).document(error.id).update({"status": "error"})
    unavailable = _ready(
        repo, uid, "https://saved.design/",
        source={"content_kind": "page", "state": "unavailable", "url": "https://saved.design/",
                "fetched_at": now, "title": "", "text": "", "limitations": []},
    )
    author = _ready(
        repo, uid, "https://www.instagram.com/reel/AbC_12/",
        source={"content_kind": "metadata", "state": "read",
                "url": "https://www.instagram.com/reel/AbC_12/",
                "title": "nike", "text": "", "fetched_at": now, "limitations": []},
    )
    poster = _ready(
        repo, uid, "https://x.com/a/status/1",
        images=[{
            "sha256": "b" * 64, "content_type": "image/jpeg", "width": 10, "height": 10,
            "byte_count": 10, "preview_kind": "video", "motion": False,
        }],
    )
    dribbble = _ready(
        repo, uid, "https://dribbble.com/shots/27186107-Shot",
        images=[{
            "sha256": "c" * 64, "content_type": "image/jpeg", "width": 10, "height": 10,
            "byte_count": 10, "preview_kind": "video", "motion": False,
        }],
    )
    code = main(["--uid", uid, "--exclude-host", "dribbble.com", "--rate", "0"])
    assert code == 0
    text = capsys.readouterr().out
    assert "mode: dry-run" in text
    assert "cohorts: error=1, unavailable=1, author_only=1, poster=1, media_incomplete=0" in text
    assert f"estimated_provider_usd: {0.0035 + 0.0035 + 0.01 + 0.01:.4f}" in text
    positions = [text.index(item) for item in (error.id, unavailable, author, poster)]
    assert positions == sorted(positions)
    assert dribbble not in text


def test_enqueue_requires_the_serving_version_and_queues_fetch(capsys):
    repo = Repository(database())
    uid = uuid4().hex
    error = repo.save(uid, str(uuid4()), Capture(kind="link", url="https://example.com/broken"))
    repo.collection(uid).document(error.id).update({"status": "error"})
    code = main([
        "--uid", uid, "--enqueue", "--scope", "fetch",
        "--require-version", ANALYSIS_VERSION, "--rate", "0", "--limit", "1",
    ])
    assert code == 0
    assert "mode: enqueue" in capsys.readouterr().out
    stored = repo.collection(uid).document(error.id).get().to_dict()
    assert stored["status"] == "queued" and stored["media_incomplete"] is False
    job = repo.job(uid, error.id).get().to_dict()
    assert job["scope"] == "fetch" and job["state"] == "pending"


def test_clip_cohort_selects_only_post_videos_without_a_stored_clip(capsys):
    repo = Repository(database())
    uid = uuid4().hex
    post = "https://x.com/a/status/{}"
    loop_only = _ready(repo, uid, post.format(1), images=[
        {"sha256": "a" * 64, "preview_kind": "video", "motion": True}])
    still = _ready(repo, uid, post.format(2), images=[
        {"sha256": "b" * 64, "preview_kind": "video", "motion": False}],
        media_incomplete=True)
    _ready(repo, uid, post.format(3), images=[
        {"sha256": "c" * 64, "preview_kind": "video", "motion": True, "clip": True}])
    _ready(repo, uid, post.format(4), images=[
        {"sha256": "d" * 64, "preview_kind": "gif", "motion": True}])
    _ready(repo, uid, post.format(5), images=[{"sha256": "e" * 64, "preview_kind": "photo"}])
    assert main(["--uid", uid, "--cohort", "clip"]) == 0
    out = capsys.readouterr().out
    assert "eligible: 2" in out and "clip=2" in out
    assert loop_only in out and still in out


def test_clip_cohort_needs_an_owner(capsys):
    import pytest

    with pytest.raises(SystemExit):
        main(["--cohort", "clip"])
    assert "--uid" in capsys.readouterr().err
