from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from io import BytesIO
from threading import Event, current_thread
from urllib.parse import quote
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from google.api_core.exceptions import ServiceUnavailable
from PIL import Image, PngImagePlugin

from queep.config import database, image_bucket
from queep.images import MAX_BYTES, Images, prepare
from queep.models import Capture


def picture(color="white", mode="RGB", size=(900, 600)):
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("private", "This must not be retained")
    Image.new(mode, size, color).save(output, format="PNG", pnginfo=metadata)
    return output.getvalue()


def media():
    return Images(database(), image_bucket())


def test_thumbnail_stays_sharp_up_to_card_size():
    prepared = prepare(picture(size=(2400, 1600)))
    assert Image.open(BytesIO(prepared.original)).size == (2400, 1600)
    assert Image.open(BytesIO(prepared.thumbnail)).size == (1440, 960)
    assert max(Image.open(BytesIO(prepare(picture()).thumbnail)).size) == 900


def test_upload_is_private_and_retrievable_in_another_session(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}/image"
    response = client.put(path, content=picture(), params={"title": "CRM branco"}, headers=headers)
    assert response.status_code == 200, response.text
    reference = response.json()
    assert reference["kind"] == "image" and reference["status"] == "queued"
    assert reference["image"]["width"] == 900
    assert reference["image"]["height"] == 600
    assert reference["analysis"] is None
    for thumbnail in (False, True):
        result = client.get(path, params={"thumbnail": thumbnail}, headers=headers)
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
        image = Image.open(BytesIO(result.content))
        assert image.size == (900, 600)
        assert "private" not in image.info and not image.getexif()
    assert client.get(path).status_code == 401
    r = httpx.post(
        "http://127.0.0.1:9099/identitytoolkit.googleapis.com/v1/accounts:signUp?key=local",
        json={"returnSecureToken": True},
    )
    assert (
        client.get(path, headers={"Authorization": f"Bearer {r.json()['idToken']}"}).status_code
        == 404
    )
    object_path = media().prefix(uid, ref_id, reference["image"]["sha256"]) + "/original"
    direct = httpx.get(
        f"http://127.0.0.1:9199/v0/b/{image_bucket().name}/o/"
        f"{quote(object_path, safe='')}?alt=media",
        headers=headers,
    )
    assert direct.status_code == 403  # Firebase clients cannot bypass API authorization.


def test_image_retries_and_kind_conflicts(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    data = picture()
    first = client.put(path + "/image", content=data, headers=headers)
    assert first.status_code == 200, first.text
    assert client.put(path + "/image", content=data, headers=headers).json() == first.json()
    assert (
        client.put(path + "/image?title=changed", content=data, headers=headers).status_code == 409
    )
    assert client.put(path + "/image", content=picture("red"), headers=headers).status_code == 409
    assert (
        client.put(path, json={"kind": "note", "text": "test"}, headers=headers).status_code == 409
    )
    edit = {"kind": "note", "text": "test", "revision": 1, "mutation_id": str(uuid4())}
    assert client.patch(path, json=edit, headers=headers).status_code == 409
    assert len(client.get("/v1/references", headers=headers).json()["items"]) == 1
    assert (
        len(
            list(
                image_bucket().list_blobs(
                    prefix=media().prefix(uid, ref_id, first.json()["image"]["sha256"])
                )
            )
        )
        == 2
    )
    assert client.delete(path + "?revision=2", headers=headers).status_code == 409
    assert client.delete(path + "?revision=1", headers=headers).status_code == 204
    assert client.get(path + "/image", headers=headers).status_code == 404
    assert client.put(path + "/image", content=data, headers=headers).status_code == 409
    assert not media().repo.job(uid, ref_id).get().exists


@pytest.mark.parametrize(
    "data,status", [(b"", 413), (b"not a PNG", 415), (b"x" * (MAX_BYTES + 1), 413)]
)
def test_invalid_images_never_become_references(client, account, data, status):
    uid, headers = account
    ref_id = str(uuid4())
    result = client.put(f"/v1/references/{ref_id}/image", content=data, headers=headers)
    assert result.status_code == status
    assert client.get("/v1/references", headers=headers).json()["items"] == []
    assert not media().manifest(uid, ref_id).get().exists


def test_static_alpha_is_preserved_and_animations_rejected():
    prepared = prepare(picture((10, 20, 30, 40), "RGBA"))
    assert prepared.info.content_type == "image/png"
    assert Image.open(BytesIO(prepared.original)).getpixel((0, 0)) == (10, 20, 30, 40)
    output = BytesIO()
    Image.new("RGB", (10, 10), "red").save(
        output,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")],
    )
    with pytest.raises(HTTPException, match="JPEG ou PNG"):
        prepare(output.getvalue())
    with pytest.raises(HTTPException):
        prepare(picture(size=(5000, 4100)))


def test_orientation_applied_and_exif_removed():
    output = BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    exif[315] = "Private author"
    Image.new("RGB", (90, 60)).save(output, format="JPEG", exif=exif)
    image = Image.open(BytesIO(prepare(output.getvalue()).original))
    assert image.size == (60, 90) and not image.getexif()


def test_interrupted_upload_retries_without_publishing_partial_content(account, monkeypatch):
    uid, _ = account
    service = media()
    ref_id = str(uuid4())
    real_put = service.put_object

    def fail_thumbnail(path, data, content_type):
        if path.endswith("/thumbnail"):
            raise ServiceUnavailable("simulated loss")
        real_put(path, data, content_type)

    monkeypatch.setattr(service, "put_object", fail_thumbnail)
    with pytest.raises(HTTPException) as failed:
        service.upload(uid, ref_id, "", picture())
    assert failed.value.status_code == 503
    assert service.repo.list(uid, 40, None).items == []
    assert not service.repo.job(uid, ref_id).get().exists
    with pytest.raises(HTTPException):
        service.repo.save(uid, ref_id, Capture(kind="note", text="Can't reuse pending image ID"))
    monkeypatch.setattr(service, "put_object", real_put)
    result = service.upload(uid, ref_id, "", picture())
    assert result.revision == 1
    assert len(service.repo.list(uid, 40, None).items) == 1


def test_deletion_failure_is_durable_and_sweeper_retries(account, monkeypatch):
    uid, _ = account
    service = media()
    ref_id = str(uuid4())
    result = service.upload(uid, ref_id, "", picture())
    service.repo.delete(uid, ref_id, result.revision)
    from google.cloud.storage.blob import Blob

    real_delete = Blob.delete
    monkeypatch.setattr(
        Blob, "delete", lambda *a, **k: (_ for _ in ()).throw(ServiceUnavailable("simulated loss"))
    )
    assert not service.cleanup(uid, ref_id)
    assert service.manifest(uid, ref_id).get().to_dict()["state"] == "cleanup"
    with pytest.raises(HTTPException):
        service.read(uid, ref_id)
    monkeypatch.setattr(Blob, "delete", real_delete)
    assert service.cleanup(uid, ref_id, datetime.now(UTC) + timedelta(minutes=11))
    assert not service.manifest(uid, ref_id).get().exists
    assert not list(
        image_bucket().list_blobs(prefix=service.prefix(uid, ref_id, result.image.sha256))
    )


def test_abandoned_upload_expires_and_late_retry_cannot_resurrect(account):
    uid, _ = account
    service = media()
    ref_id = str(uuid4())
    data = picture()
    prepared = prepare(data)
    service.reserve(uid, ref_id, "", prepared)
    prefix = service.prefix(uid, ref_id, prepared.info.sha256)
    service.put_object(prefix + "/original", prepared.original, prepared.info.content_type)
    assert service.cleanup(uid, ref_id, datetime.now(UTC) + timedelta(hours=2))
    with pytest.raises(HTTPException) as failed:
        service.upload(uid, ref_id, "", data)
    assert failed.value.status_code == 409
    assert service.repo.list(uid, 40, None).items == []
    assert not list(image_bucket().list_blobs(prefix=prefix))


def test_late_upload_cannot_publish_after_concurrent_retry_and_delete(account, monkeypatch):
    uid, _ = account
    service = media()
    ref_id = str(uuid4())
    paused, resume = Event(), Event()
    real_put = service.put_object

    def interleaved_put(path, data, content_type):
        if current_thread().name.startswith("late-upload") and path.endswith("/thumbnail"):
            paused.set()
            assert resume.wait(15)
        real_put(path, data, content_type)

    monkeypatch.setattr(service, "put_object", interleaved_put)
    with ThreadPoolExecutor(1, thread_name_prefix="late-upload") as pool:
        late = pool.submit(service.upload, uid, ref_id, "", picture())
        try:
            assert paused.wait(15)
            reference = service.upload(uid, ref_id, "", picture())
            service.repo.delete(uid, ref_id, reference.revision)
            assert service.cleanup(uid, ref_id)
        finally:
            resume.set()
        with pytest.raises(HTTPException) as failed:
            late.result()
        assert failed.value.status_code == 409
    assert service.repo.list(uid, 40, None).items == []
    assert not list(
        image_bucket().list_blobs(prefix=service.prefix(uid, ref_id, reference.image.sha256))
    )


def test_image_title_and_notes_edit_keeps_file_and_requeues(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    first = client.put(path + "/image?title=Quadro", content=picture(), headers=headers)
    assert first.status_code == 200, first.text
    edit = {"kind": "image", "title": "Quadro do workshop", "text": "Ideias de **onboarding**",
            "revision": 1, "mutation_id": str(uuid4())}
    edited = client.patch(path, json=edit, headers=headers)
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert (body["title"], body["text"], body["revision"], body["status"]) == (
        "Quadro do workshop", "Ideias de **onboarding**", 2, "queued")
    assert body["image"] == first.json()["image"]
    assert body["analysis"] is None
    assert client.patch(path, json=edit, headers=headers).json() == body
    fresh = {**edit, "mutation_id": str(uuid4())}
    assert client.patch(path, json=fresh, headers=headers).status_code == 409
    wrong_kind = {**fresh, "kind": "audio", "revision": 2}
    assert client.patch(path, json=wrong_kind, headers=headers).status_code == 409
    assert media().repo.job(uid, ref_id).get().to_dict()["revision"] == 2
    assert client.get(path + "/image", headers=headers).status_code == 200
    assert client.get(path, headers=headers).json()["text"] == "Ideias de **onboarding**"
    assert client.delete(path + "?revision=1", headers=headers).status_code == 409
    assert client.delete(path + "?revision=2", headers=headers).status_code == 204


def test_attach_preview_keeps_link_kind_and_serves_the_photo(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    created = client.put(
        path,
        json={"kind": "link", "url": "https://x.com/richardrx/status/2099853605054316775"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    assert created.json()["kind"] == "link" and created.json()["image"] is None
    info = media().attach_preview(uid, ref_id, picture())
    assert info.width == 900 and info.height == 600
    record = client.get(path, headers=headers).json()
    assert record["kind"] == "link" and record["image"]["width"] == 900
    photo = client.get(path + "/image", headers=headers)
    assert photo.status_code == 200
    assert Image.open(BytesIO(photo.content)).size == (900, 600)
    second = media().attach_preview(uid, ref_id, picture("red"))
    assert second.width == 900
    record = client.get(path, headers=headers).json()
    assert len(record["images"]) == 2
    assert record["image"]["sha256"] == record["images"][0]["sha256"]
    first_bytes = client.get(path + "/image", params={"index": 0}, headers=headers).content
    other = client.get(path + "/image", params={"index": 1}, headers=headers)
    assert other.status_code == 200
    assert other.content != first_bytes
    assert client.get(path + "/image", params={"index": 2}, headers=headers).status_code == 404


def test_attach_preview_marks_video_poster(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    created = client.put(
        path,
        json={"kind": "link", "url": "https://x.com/a/status/2099804636328648823"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    info = media().attach_preview(uid, ref_id, picture("blue"), preview_kind="video")
    assert info.preview_kind == "video"
    record = client.get(path, headers=headers).json()
    assert record["images"][0]["preview_kind"] == "video"


def test_attach_author_is_served_apart_from_the_gallery(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    created = client.put(
        path,
        json={"kind": "link", "url": "https://x.com/ada/status/2099853605054316775"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    avatar = media().attach_author(uid, ref_id, picture("green"))
    preview = media().attach_preview(uid, ref_id, picture("red"))
    record = client.get(path, headers=headers).json()
    assert record["author_image"]["sha256"] == avatar.sha256
    assert record["image"]["sha256"] == preview.sha256
    assert record["images"][0]["sha256"] == preview.sha256
    gallery = client.get(path + "/image", headers=headers)
    author = client.get(path + "/image", params={"role": "author"}, headers=headers)
    assert gallery.status_code == 200 and author.status_code == 200
    assert gallery.content != author.content


def test_link_title_edit_does_not_requeue(client, account):
    uid, headers = account
    ref_id = str(uuid4())
    path = f"/v1/references/{ref_id}"
    created = client.put(
        path,
        json={"kind": "link", "title": "", "url": "https://x.com/ada/status/2099853605054316775"},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    edit = {"kind": "link", "title": "Mixer no estúdio", "text": "",
            "revision": created.json()["revision"], "mutation_id": str(uuid4())}
    edited = client.patch(path, json=edit, headers=headers)
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["title"] == "Mixer no estúdio"
    assert body["status"] == "queued"
    assert body["revision"] == created.json()["revision"] + 1
    assert media().repo.job(uid, ref_id).get().to_dict()["revision"] == created.json()["revision"]
