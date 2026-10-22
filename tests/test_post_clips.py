"""A post's full video with its sound: kept on Pro and Max, served in ranges, charged once."""

from uuid import uuid4

import pytest
from fastapi import HTTPException

from queep.billing import Billing
from queep.config import Settings, database, image_bucket
from queep.images import Images
from queep.main import clip_range
from queep.media import playable_clip
from queep.usage import inventory_document
from tests.test_images import picture
from tests.test_media import video  # noqa: F401 (fixture)
from tests.test_plans import change_plan

POST = "https://x.com/a/status/2099804636328648823"
LOOP = b"muted-loop" + b"\x00" * 48


@pytest.fixture
def plan(monkeypatch):
    """The provider's answer for the account, as in `tests/test_plans.py`."""
    from datetime import timedelta

    from queep.billing import Access

    current = {"value": "free"}
    monkeypatch.setenv("QUEEP_BILLING_MODE", "required")
    monkeypatch.setattr(Billing, "customer", lambda self, uid, now: Access(
        active=current["value"] != "free", plan=current["value"], checked_at=now,
        expires_at=now + timedelta(days=30) if current["value"] != "free" else None))
    return current


def media():
    return Images(database(), image_bucket())


def post(client, headers):
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    created = client.put(path, json={"kind": "link", "url": POST}, headers=headers)
    assert created.status_code == 200, created.text
    return ref_id, path


def stored(uid):
    return (inventory_document(database(), uid).get().to_dict() or {}).get("storage_bytes", 0)


def on(plan_state, uid, value):
    change_plan(plan_state, uid, value)
    Billing(database(), Settings()).access(uid, force=True)


@pytest.mark.parametrize("value", ["pro", "max"])
def test_paid_plans_keep_the_clip_and_serve_it_in_ranges(client, account, plan, value):
    uid, headers = account
    on(plan, uid, value)
    ref_id, path = post(client, headers)
    clip = bytes(range(256)) * 40
    before = stored(uid)
    info = media().attach_preview(uid, ref_id, picture("blue"), preview_kind="video",
                                  motion=LOOP, clip=clip)
    assert info.clip is True and info.motion is True
    record = client.get(path, headers=headers).json()
    assert record["images"][0]["clip"] is True
    assert stored(uid) - before >= len(clip) + len(LOOP)

    whole = client.get(path + "/image", params={"role": "clip"}, headers=headers)
    assert whole.status_code == 200
    assert whole.headers["content-type"].startswith("video/mp4")
    assert whole.headers["accept-ranges"] == "bytes"
    assert whole.content == clip

    first = client.get(path + "/image", params={"role": "clip"},
                       headers={**headers, "Range": "bytes=0-99"})
    assert first.status_code == 206
    assert first.headers["content-range"] == f"bytes 0-99/{len(clip)}"
    assert first.content == clip[:100]

    tail = client.get(path + "/image", params={"role": "clip"},
                      headers={**headers, "Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == clip[-10:]

    open_ended = client.get(path + "/image", params={"role": "clip"},
                            headers={**headers, "Range": f"bytes={len(clip) - 5}-"})
    assert open_ended.status_code == 206 and open_ended.content == clip[-5:]

    beyond = client.get(path + "/image", params={"role": "clip"},
                        headers={**headers, "Range": f"bytes={len(clip)}-"})
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{len(clip)}"

    # The loop and the still are unchanged.
    assert client.get(path + "/image", params={"motion": "true"},
                      headers=headers).content == LOOP
    assert client.get(path + "/image", params={"role": "clip", "motion": "true"},
                      headers=headers).status_code == 404


def test_free_keeps_only_the_loop(client, account, plan):
    uid, headers = account
    on(plan, uid, "free")
    ref_id, path = post(client, headers)
    before = stored(uid)
    info = media().attach_preview(uid, ref_id, picture("blue"), preview_kind="video",
                                  motion=LOOP, clip=b"\x01" * 4096)
    assert info.clip is False and info.motion is True
    assert client.get(path, headers=headers).json()["images"][0]["clip"] is False
    assert client.get(path + "/image", params={"role": "clip"},
                      headers=headers).status_code == 404
    service = media()
    poster = sum(len(data) for _, data, _ in service.objects(service.prepare(picture("blue"))))
    assert stored(uid) - before == poster + len(LOOP)
    prefix = service.prefix(uid, ref_id, info.sha256)
    assert not image_bucket().blob(f"{prefix}/clip").exists()


def test_gifs_and_photos_never_keep_a_clip(client, account, plan):
    uid, headers = account
    on(plan, uid, "max")
    ref_id, _ = post(client, headers)
    gif = media().attach_preview(uid, ref_id, picture("red"), preview_kind="gif",
                                 motion=LOOP, clip=b"\x01" * 4096)
    photo = media().attach_preview(uid, ref_id, picture("green"), preview_kind="photo",
                                   clip=b"\x01" * 4096)
    assert gif.clip is False and photo.clip is False


def test_clip_added_to_a_stored_poster_is_charged_once(client, account, plan):
    uid, headers = account
    on(plan, uid, "pro")
    ref_id, path = post(client, headers)
    still = media().attach_preview(uid, ref_id, picture("blue"), preview_kind="video",
                                   motion=LOOP)
    assert still.clip is False
    clip = b"\x02" * 5000
    before = stored(uid)
    upgraded = media().attach_preview(uid, ref_id, picture("blue"), preview_kind="video",
                                      motion=LOOP, clip=clip)
    assert upgraded.sha256 == still.sha256
    assert upgraded.clip is True and upgraded.motion is True
    assert stored(uid) - before == len(clip)
    again = media().attach_preview(uid, ref_id, picture("blue"), preview_kind="video",
                                   motion=LOOP, clip=clip)
    assert again.clip is True
    assert stored(uid) - before == len(clip)
    record = client.get(path, headers=headers).json()
    assert len(record["images"]) == 1 and record["images"][0]["clip"] is True
    assert client.get(path + "/image", params={"role": "clip"},
                      headers=headers).content == clip


def test_a_full_plan_still_gets_the_poster_and_loop(client, account, plan):
    uid, headers = account
    on(plan, uid, "pro")
    ref_id, path = post(client, headers)
    limit = 2048 * 1024 * 1024
    inventory_document(database(), uid).set(
        {"items": 1, "storage_bytes": limit - 200 * 1024, "spaces": 0})
    info = media().attach_preview(uid, ref_id, picture("blue"), preview_kind="video",
                                  motion=LOOP, clip=b"\x03" * (1024 * 1024))
    assert info.clip is False and info.motion is True
    assert client.get(path + "/image", params={"role": "clip"},
                      headers=headers).status_code == 404
    assert client.get(path + "/image", params={"motion": "true"},
                      headers=headers).content == LOOP


def test_deleting_the_post_removes_the_clip(client, account, plan):
    uid, headers = account
    on(plan, uid, "max")
    ref_id, _ = post(client, headers)
    service = media()
    info = service.attach_preview(uid, ref_id, picture("blue"), preview_kind="video",
                                  motion=LOOP, clip=b"\x04" * 2048)
    prefix = service.prefix(uid, ref_id, info.sha256)
    assert image_bucket().blob(f"{prefix}/clip").exists()
    record = service.repo.get(uid, ref_id)
    service.repo.delete(uid, ref_id, record.revision)
    assert service.cleanup(uid, ref_id)
    assert not list(image_bucket().list_blobs(prefix=prefix))


def test_playable_clip_accepts_h264_with_sound_and_rejects_anything_else(video):  # noqa: F811
    assert playable_clip(video) == video
    assert playable_clip(b"") is None
    assert playable_clip(b"not a video" * 10) is None
    assert playable_clip(b"\x00" * (25 * 1024 * 1024 + 1)) is None


@pytest.mark.parametrize("header,expected", [
    (None, None),
    ("bytes=0-99", (0, 99)),
    ("bytes=10-", (10, 999)),
    ("bytes=-100", (900, 999)),
    ("bytes=-5000", (0, 999)),
    ("bytes=990-5000", (990, 999)),
    ("bytes=0-1, 5-9", (0, 1)),
])
def test_clip_range_spans(header, expected):
    assert clip_range(header, 1000) == expected


def test_clip_range_caps_one_chunk():
    start, end = clip_range("bytes=0-", 50 * 1024 * 1024)
    assert start == 0 and end == 4 * 1024 * 1024 - 1


@pytest.mark.parametrize("header", [
    "bytes=1000-", "items=0-1", "bytes=5-2", "bytes=-", "bytes=a-b",
])
def test_clip_range_rejects_what_it_cannot_serve(header):
    with pytest.raises(HTTPException) as error:
        clip_range(header, 1000)
    assert error.value.status_code == 416
