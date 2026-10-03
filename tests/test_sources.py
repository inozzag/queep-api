import asyncio
import gzip
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from queep import sources


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
        "http://224.0.0.1/",
        "http://100.64.0.1/",
        "http://localhost/",
        "http://metadata.google.internal/",
        "http://myhost.local/",
        "http://public.example.com:8080/",
        "https://user:secret@example.com/",
        "file:///etc/passwd",
        "https://example.com\\@127.0.0.1/",
        "https://example.com/\r\nX:1",
    ],
)
def test_nonpublic_addresses_never_connect(monkeypatch, url):
    async def fail(*_):
        pytest.fail("An unsafe destination reached the network")

    monkeypatch.setattr(sources, "request", fail)
    result = sources.read_source(url)
    assert result.state == "unavailable" and not result.text


def test_redirects_are_revalidated_and_dns_answer_is_pinned(monkeypatch):
    seen = []

    async def resolve(host):
        seen.append(("dns", host))
        return ["93.184.215.14"] if len(seen) == 1 else ["127.0.0.1"]

    async def request(url, address):
        seen.append((str(url), address))
        return 302, {"location": "/next"}, b""

    monkeypatch.setattr(sources, "resolve", resolve)
    monkeypatch.setattr(sources, "request", request)
    assert sources.read_source("https://example.com").state == "unavailable"
    assert seen == [
        ("dns", "example.com"),
        ("https://example.com", "93.184.215.14"),
        ("dns", "example.com"),
    ]


def test_mixed_dns_answer_is_rejected(monkeypatch):
    async def resolve(_):
        return ["93.184.215.14", "10.0.0.1"]

    monkeypatch.setattr(sources, "resolve", resolve)
    assert sources.read_source("https://example.com").state == "unavailable"


@pytest.mark.parametrize("location", ["http://example.com/", "http://169.254.169.254/", "/loop"])
def test_downgrade_metadata_and_redirect_loops_stop(monkeypatch, location):
    seen = []

    async def resolve(_):
        return ["93.184.215.14"]

    async def request(url, address):
        seen.append(str(url))
        return 302, {"location": location}, b""

    monkeypatch.setattr(sources, "resolve", resolve)
    monkeypatch.setattr(sources, "request", request)
    assert sources.read_source("https://example.com").state == "unavailable"
    assert len(seen) == (5 if location == "/loop" else 1)


def test_total_deadline_cancels_slow_source(monkeypatch):
    cancelled = []

    async def resolve(_):
        try:
            await asyncio.sleep(2)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(sources, "resolve", resolve)
    monkeypatch.setattr(sources, "TOTAL_TIMEOUT", 0.01)
    result = sources.read_source("https://example.com")
    assert result.state == "unavailable" and cancelled


def test_transient_failure_retries_once_within_the_same_deadline(monkeypatch):
    attempts = []

    async def fetch(_):
        attempts.append(True)
        if len(attempts) == 1:
            raise sources.SourceError("transient")
        return sources.extract(
            b"Public article " * 10, {"content-type": "text/plain"}, "https://example.com"
        )

    monkeypatch.setattr(sources, "fetch", fetch)
    assert sources.read_source("https://example.com").state == "read"
    assert len(attempts) == 2


def pool(monkeypatch, body=b"", headers=None, status=200):
    seen = []

    class Pool:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            seen.append({"method": method, "url": url, **kwargs})

            async def chunks():
                yield body

            yield SimpleNamespace(
                status=status,
                aiter_stream=chunks,
                headers=headers or [(b"content-type", b"text/html")],
            )

    monkeypatch.setattr(sources.httpcore, "AsyncConnectionPool", Pool)
    return seen


def test_connection_uses_validated_ip_but_original_host_and_tls_name(monkeypatch):
    seen = pool(monkeypatch)
    url = sources.checked_url("https://example.com/read?q=1")
    asyncio.run(sources.request(url, "93.184.215.14"))
    assert seen[0]["url"] == "https://93.184.215.14/read?q=1"
    assert seen[0]["headers"]["Host"] == "example.com"
    assert seen[0]["extensions"]["sni_hostname"] == "example.com"
    assert "Cookie" not in seen[0]["headers"] and "Authorization" not in seen[0]["headers"]


@pytest.mark.parametrize(
    "body,headers,status,reason",
    [
        (b"", [(b"content-type", b"video/mp4")], 200, "format"),
        (b"", [(b"content-type", b"text/html"), (b"content-encoding", b"gzip")], 200, "format"),
        (b"", [(b"content-type", b"text/html"), (b"content-length", b"99999999")], 200, "size"),
        (b"x" * (sources.MAX_BODY + 1), None, 200, "size"),
        (b"private content", None, 403, "http"),
    ],
)
def test_response_limits_before_parsing(monkeypatch, body, headers, status, reason):
    pool(monkeypatch, body, headers, status)
    with pytest.raises(sources.SourceError, match=reason):
        asyncio.run(sources.request(sources.checked_url("https://example.com/"), "93.184.215.14"))


def test_gzip_page_is_decoded_with_a_decompression_limit(monkeypatch):
    headers = [(b"content-type", b"text/html"), (b"content-encoding", b"gzip")]
    body = b"Readable article " * 100
    pool(monkeypatch, gzip.compress(body), headers)
    _, _, decoded = asyncio.run(
        sources.request(sources.checked_url("https://example.com"), "93.184.215.14")
    )
    assert decoded == body
    pool(monkeypatch, gzip.compress(b"x" * (sources.MAX_BODY + 1)), headers)
    with pytest.raises(sources.SourceError, match="size"):
        asyncio.run(sources.request(sources.checked_url("https://example.com"), "93.184.215.14"))


def test_github_readme_excludes_navigation_scripts_and_hidden_text():
    page = b"""<html><title>TestKit</title><body><nav>Private nav</nav><main>Site chrome
    <article class="markdown-body"><h1>TestKit</h1><p>A repository of components for
    building native SwiftUI interfaces on iPhone and Mac, with forms and navigation.</p>
    <script>inject instructions</script><span hidden>hidden text</span></article></main></body>"""
    result = sources.extract(page, {"content-type": "text/html"}, "https://github.com/a/b")
    assert result.state == "read" and "SwiftUI" in result.text
    assert all(word not in result.text for word in ["Private", "chrome", "inject", "hidden"])
    assert result.title == "TestKit" and result.limitations and not result.truncated
    assert result.content_kind == "page" and result.native_title is False
    assert result.fetched_at <= datetime.now(UTC)


def test_long_utf8_page_is_bounded_and_labelled():
    result = sources.extract(
        ("conteúdo " * 10000).encode(),
        {"content-type": "text/plain"},
        "https://example.com/readme.txt",
    )
    assert result.truncated and len(result.text.encode()) <= sources.MAX_TEXT
    assert len(result.limitations) == 2 and "�" not in result.text


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><script>loadApp()</script></body></html>",
        "<title>Just a moment...</title><body>Challenge</body>",
    ],
)
def test_unreadable_pages_are_not_fabricated(html):
    with pytest.raises(sources.SourceError, match="empty"):
        sources.extract(html.encode(), {"content-type": "text/html"}, "https://example.com")


def test_query_evidence_can_find_a_passage_beyond_intro():
    text = "x" * 800 + " SwiftUI native application components " + "x" * 400
    assert "SwiftUI" in sources.evidence(text, "native SwiftUI components")


@pytest.mark.parametrize(
    "url",
    [
        "https://youtu.be/M7lc1UVf-VE",
        "https://www.youtube.com/shorts/M7lc1UVf-VE",
        "https://www.youtube.com/watch?v=M7lc1UVf-VE&t=10",
    ],
)
def test_video_metadata_endpoint_contains_only_a_canonical_public_id(url):
    endpoint = sources.social_endpoint(sources.checked_url(url))
    assert endpoint.startswith("https://www.youtube.com/oembed?")
    assert "M7lc1UVf-VE" in endpoint and "t%3D10" not in endpoint
    assert (
        sources.social_endpoint(
            sources.checked_url("https://youtube.com.example.org/watch?v=M7lc1UVf-VE")
        )
        is None
    )
    assert sources.social_endpoint(sources.checked_url("https://youtu.be/127.0.0.1")) is None


def test_metadata_does_not_fetch_embed_html_images_or_media():
    result = sources.video_metadata(
        b'{"title":"Native app demo","author_name":"Author",'
        b'"html":"<script>bad()</script>",'
        b'"thumbnail_url":"http://127.0.0.1/private"}',
        sources.checked_url("https://youtu.be/M7lc1UVf-VE"),
    )
    assert result.content_kind == "metadata" and result.state == "read"
    assert "Native app demo" in result.text and "bad" not in result.text
    assert "127.0.0.1" not in result.text and result.limitations
    posters = sources.video_posters(
        b'{"title":"Native app demo","author_name":"Author",'
        b'"thumbnail_url":"http://127.0.0.1/private"}',
        sources.checked_url("https://youtu.be/M7lc1UVf-VE"),
    )
    assert posters == [{"url": "https://www.youtube.com/favicon.ico", "kind": "avatar"}]
    posters = sources.video_posters(
        b'{"title":"Native app demo","author_name":"Author",'
        b'"thumbnail_url":"https://i.ytimg.com/vi/M7lc1UVf-VE/hqdefault.jpg"}',
        sources.checked_url("https://youtu.be/M7lc1UVf-VE"),
    )
    assert posters[0]["kind"] == "video" and posters[0]["url"].endswith("hqdefault.jpg")


def test_instagram_description_is_partial_not_a_transcript():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    url = sources.checked_url("https://www.instagram.com/p/example/")
    match = adapter.match(url)
    assert match.role == "post" and match.key == "example"
    result = adapter.parse(
        b'<html><title>Post</title><meta property="og:description" '
        b'content="Three tips for building native applications."/></html>',
        url,
        match,
    )
    assert result.content_kind == "metadata"
    assert "Three tips" in result.text and "não foram analisados" in result.limitations[0]


def test_instagram_matches_posts_reels_and_share_not_stories():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()

    def match(url):
        return adapter.match(sources.checked_url(url))

    assert match("https://www.instagram.com/p/AbC_12/").key == "AbC_12"
    assert match("https://www.instagram.com/reel/AbC_12/").role == "post"
    assert match("https://instagram.com/ada/reel/AbC_12/").key == "AbC_12"
    assert match("https://www.instagram.com/share/reel/AbC_12/").key == "AbC_12"
    assert match("https://www.instagram.com/stories/ada/1") is None
    profile = match("https://www.instagram.com/ada.lovelace/")
    assert profile.role == "profile" and profile.key == "ada.lovelace"
    assert match("https://www.instagram.com/explore/") is None


def test_instagram_carousel_keeps_order_and_skips_private_hosts():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    url = sources.checked_url("https://www.instagram.com/p/carousel1/")
    match = adapter.match(url)
    cdn = "https://scontent.cdninstagram.com"

    def slide(name):
        return {"node": {"display_url": f"{cdn}/{name}", "is_video": False}}

    body = json.dumps({
        "data": {"xdt_shortcode_media": {
            "__typename": "GraphSidecar",
            "owner": {"username": "ada", "full_name": "Ada",
                      "profile_pic_url": f"{cdn}/avatar.jpg"},
            "edge_media_to_caption": {"edges": [{"node": {"text": "Tres fotos do estudo."}}]},
            "edge_sidecar_to_children": {"edges": [
                slide("one.jpg"),
                {"node": {"display_url": "http://127.0.0.1/private.jpg", "is_video": False}},
                slide("two.jpg"),
                slide("three.jpg"),
                slide("four.jpg"),
                slide("five.jpg"),
            ]},
        }},
    }).encode()
    source = adapter.parse(body, url, match)
    media = adapter.media(body, url, match)
    assert source.author_handle == "ada" and "estudo" in source.text
    assert source.native_title is False
    assert [item["url"] for item in media if item["kind"] == "photo"] == [
        f"{cdn}/one.jpg",
        f"{cdn}/two.jpg",
        f"{cdn}/three.jpg",
        f"{cdn}/four.jpg",
    ]
    assert media[0]["kind"] == "avatar"
    assert "127.0.0.1" not in str(media)


def test_instagram_reel_keeps_poster_and_public_clip():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    url = sources.checked_url("https://www.instagram.com/reel/clip1/")
    match = adapter.match(url)
    body = json.dumps({
        "shortcode_media": {
            "__typename": "GraphVideo",
            "is_video": True,
            "display_url": "https://scontent.cdninstagram.com/poster.jpg",
            "video_url": "https://scontent.cdninstagram.com/reel.mp4",
            "owner": {"username": "ada", "full_name": "Ada Lovelace"},
            "edge_media_to_caption": {"edges": [{"node": {"text": "Um mixer no estudio."}}]},
        },
    }).encode()
    source = adapter.parse(body, url, match)
    media = adapter.media(body, url, match)
    assert source.author_name.startswith("Ada")
    assert "mixer" in source.text
    assert media[-1] == {
        "url": "https://scontent.cdninstagram.com/poster.jpg",
        "kind": "video",
        "clip_url": "https://scontent.cdninstagram.com/reel.mp4",
    }


def test_instagram_mixed_carousel_clip_only_on_first_video():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    url = sources.checked_url("https://www.instagram.com/p/mix1/")
    match = adapter.match(url)
    body = json.dumps({
        "data": {"graphql": {"shortcode_media": {
            "edge_sidecar_to_children": {"edges": [
                {"node": {
                    "display_url": "https://scontent.cdninstagram.com/a.jpg",
                    "is_video": False,
                }},
                {"node": {
                    "display_url": "https://scontent.cdninstagram.com/b.jpg",
                    "video_url": "https://scontent.cdninstagram.com/first.mp4",
                    "is_video": True,
                }},
                {"node": {
                    "display_url": "https://scontent.cdninstagram.com/c.jpg",
                    "video_url": "https://scontent.cdninstagram.com/second.mp4",
                    "is_video": True,
                }},
            ]},
            "owner": {"username": "ada"},
            "edge_media_to_caption": {"edges": [{"node": {"text": "Foto e dois clips."}}]},
        }}},
    }).encode()
    media = [item for item in adapter.media(body, url, match) if item["kind"] != "avatar"]
    assert media[0]["kind"] == "photo"
    assert media[1]["clip_url"].endswith("first.mp4")
    assert "clip_url" not in media[2]
    assert adapter.parse(body, url, match).author_handle == "ada"


def test_instagram_profile_is_not_a_post():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    profile = adapter.match(sources.checked_url("https://www.instagram.com/ada.lovelace/"))
    assert profile.role == "profile" and profile.key == "ada.lovelace"
    assert adapter.match(sources.checked_url("https://www.instagram.com/explore/")) is None


@pytest.mark.parametrize(
    "url,role,key",
    [
        ("https://dribbble.com/shots/27064263-Hero-Screen", "post", "27064263"),
        ("https://www.dribbble.com/shots/123", "post", "123"),
        ("https://dribbble.com/ada-design", "profile", "ada-design"),
        ("https://portfolio.dribbble.com/Ada_Design/", "profile", "Ada_Design"),
    ],
)
def test_dribbble_matches_shots_and_profiles(url, role, key):
    from queep.social.dribbble import DribbbleAdapter

    matched = DribbbleAdapter().match(sources.checked_url(url))
    assert matched.network == "dribbble" and matched.role == role and matched.key == key


@pytest.mark.parametrize(
    "url",
    [
        "https://dribbble.com/shots",
        "https://dribbble.com/shots/not-a-number",
        "https://dribbble.com/shots/123-title/attachments/1",
        "https://dribbble.com/designers",
        "https://dribbble.com/search",
        "https://dribbble.com/signup",
        "https://dribbble.com/session",
        "https://dribbble.com/jobs",
        "https://dribbble.com/ada/collections",
        "https://dribbble.com.example.org/shots/123-title",
    ],
)
def test_dribbble_rejects_reserved_noncanonical_and_spoofed_urls(url):
    from queep.social.dribbble import DribbbleAdapter

    assert DribbbleAdapter().match(sources.checked_url(url)) is None


def test_dribbble_shot_readers_prefer_official_oembed_then_html():
    from queep.social.dribbble import DribbbleAdapter

    adapter = DribbbleAdapter()
    url = sources.checked_url(
        "https://www.dribbble.com/shots/123-Public-Shot?utm_source=queep"
    )
    readers = adapter.readers(url, adapter.match(url))
    assert readers == [
        "https://dribbble.com/services/oembed?url="
        "https%3A%2F%2Fdribbble.com%2Fshots%2F123-Public-Shot",
        "https://dribbble.com/shots/123-Public-Shot",
        "https://r.jina.ai/https://dribbble.com/shots/123-Public-Shot",
    ]


def test_dribbble_oembed_keeps_public_author_text_and_media_only():
    from queep.social.dribbble import DribbbleAdapter

    adapter = DribbbleAdapter()
    url = sources.checked_url("https://dribbble.com/shots/123-Public-Shot")
    matched = adapter.match(url)
    body = (
        b'{"type":"video","title":"Public Shot","author_name":"Ada Lovelace",'
        b'"author_url":"https://dribbble.com/ada",'
        b'"description":"<p>Uma interface para equipes.</p><script>bad()</script>",'
        b'"author_avatar_url":"https://cdn.dribbble.com/users/1/avatar.png",'
        b'"thumbnail_url":"https://cdn.dribbble.com/users/1/shot.png",'
        b'"video_url":"https://cdn.dribbble.com/users/1/shot.mp4",'
        b'"image":"http://127.0.0.1/private.png"}'
    )
    result = adapter.parse(body, url, matched)
    previews = adapter.media(body, url, matched)
    assert result.content_kind == "metadata" and result.native_title is False
    assert result.title == "Ada Lovelace" and result.author_handle == "ada"
    assert result.text == "Uma interface para equipes."
    assert "script" not in result.text and "127.0.0.1" not in result.text
    assert previews == [
        {"url": "https://cdn.dribbble.com/users/1/avatar.png", "kind": "avatar"},
        {
            "url": "https://cdn.dribbble.com/users/1/shot.png",
            "kind": "video",
            "clip_url": "https://cdn.dribbble.com/users/1/shot.mp4",
        },
    ]


def test_dribbble_html_reads_shot_description_and_bounded_public_gallery():
    from queep.social.dribbble import DribbbleAdapter

    adapter = DribbbleAdapter()
    url = sources.checked_url("https://dribbble.com/shots/123-Public-Shot")
    matched = adapter.match(url)
    body = b"""<html><head>
        <meta property="og:title" content="Public Shot by Ada Lovelace on Dribbble">
        <meta property="og:description" content="Descricao publica do shot.">
        <meta name="author:image" content="https://cdn.dribbble.com/avatar.png">
        <meta property="og:image" content="https://cdn.dribbble.com/one.png">
        <meta property="og:image" content="http://127.0.0.1/private.png">
        <meta property="og:image" content="https://cdn.dribbble.com/two.png">
        <meta property="og:video" content="https://cdn.dribbble.com/clip.mp4">
        <meta property="og:video:type" content="video/mp4">
        </head><body><a rel="author" href="/ada">Ada Lovelace</a>
        <script>private()</script></body></html>"""
    result = adapter.parse(body, url, matched)
    previews = adapter.media(body, url, matched)
    assert result.author_name == "Ada Lovelace" and result.author_handle == "ada"
    assert result.text == "Descricao publica do shot."
    assert previews == [
        {"url": "https://cdn.dribbble.com/avatar.png", "kind": "avatar"},
        {
            "url": "https://cdn.dribbble.com/one.png",
            "kind": "video",
            "clip_url": "https://cdn.dribbble.com/clip.mp4",
        },
        {"url": "https://cdn.dribbble.com/two.png", "kind": "photo"},
    ]


def test_dribbble_mirror_html_reads_designer_caption_and_shot_images():
    from queep.social.dribbble import DribbbleAdapter

    adapter = DribbbleAdapter()
    url = sources.checked_url("https://dribbble.com/shots/123-Public-Shot")
    matched = adapter.match(url)
    body = b"""<html><head>
        <meta property="og:title" content="Public Shot">
        <meta name="twitter:data1" content="@ada">
        <meta property="og:image" content="https://cdn.dribbble.com/userupload/1/file/shot.png">
        <meta property="og:image" content="https://cdn.dribbble.com/uploads/9/original/logo.png">
        </head><body>
        <header class="user-sticky-header">
        <a class="user-sticky-header__avatar" href="/ada">
        <img src="https://cdn.dribbble.com/users/1/avatars/normal/a.jpg">
        </a>
        <a class="hoverable" href="/ada">Ada Lovelace</a>
        </header>
        <div class="formatted-text"><p>Uma interface para equipes.</p></div>
        </body></html>"""
    result = adapter.parse(body, url, matched)
    assert result.author_name == "Ada Lovelace" and result.author_handle == "ada"
    assert result.text == "Uma interface para equipes."
    assert adapter.media(body, url, matched) == [
        {"url": "https://cdn.dribbble.com/users/1/avatars/normal/a.jpg", "kind": "avatar"},
        {"url": "https://cdn.dribbble.com/userupload/1/file/shot.png", "kind": "photo"},
    ]


def test_dribbble_profile_keeps_avatar_out_of_shot_gallery_and_allows_empty_bio():
    from queep.social.dribbble import DribbbleAdapter

    adapter = DribbbleAdapter()
    url = sources.checked_url("https://dribbble.com/ada")
    matched = adapter.match(url)
    body = b"""<html><head>
        <title>Ada Lovelace | Dribbble</title>
        <meta property="og:title" content="Ada Lovelace | Dribbble">
        <meta property="og:description" content="">
        <meta property="og:image" content="https://cdn.dribbble.com/avatar.png">
        </head><body></body></html>"""
    result = adapter.parse(body, url, matched)
    assert result.content_kind == "profile" and result.native_title is True
    assert result.title == "Ada Lovelace" and result.author_handle == "ada"
    assert result.text == ""
    assert adapter.media(body, url, matched) == [
        {"url": "https://cdn.dribbble.com/avatar.png", "kind": "avatar"}
    ]


@pytest.mark.parametrize(
    "url,role,key",
    [
        ("https://www.behance.net/gallery/210524379/Reel-2024", "post", "210524379"),
        ("https://behance.net/gallery/123/Title/modules/9", "post", "123"),
        ("https://www.behance.net/ada-design", "profile", "ada-design"),
        ("https://be.net/Ada_Design/", "profile", "Ada_Design"),
    ],
)
def test_behance_matches_projects_and_profiles(url, role, key):
    from queep.social.behance import BehanceAdapter

    matched = BehanceAdapter().match(sources.checked_url(url))
    assert matched.network == "behance" and matched.role == role and matched.key == key


@pytest.mark.parametrize(
    "url",
    [
        "https://www.behance.net/gallery",
        "https://www.behance.net/gallery/not-a-number",
        "https://www.behance.net/galleries",
        "https://www.behance.net/search",
        "https://www.behance.net/hire",
        "https://www.behance.net/joblist",
        "https://www.behance.net/ada/services/1",
        "https://www.behance.net/ada/projects",
        "https://behance.net.example.org/gallery/123/Title",
    ],
)
def test_behance_rejects_reserved_services_and_spoofed_urls(url):
    from queep.social.behance import BehanceAdapter

    assert BehanceAdapter().match(sources.checked_url(url)) is None


def test_behance_project_readers_prefer_official_oembed_then_html():
    from queep.social.behance import BehanceAdapter

    adapter = BehanceAdapter()
    url = sources.checked_url("https://www.behance.net/gallery/123/Public-Project")
    readers = adapter.readers(url, adapter.match(url))
    assert readers == [
        "https://www.behance.net/services/oembed?url="
        "https%3A%2F%2Fwww.behance.net%2Fgallery%2F123%2FPublic-Project",
        "https://www.behance.net/gallery/123/Public-Project",
    ]


def test_behance_oembed_keeps_public_author_text_and_skips_embed_html():
    from queep.social.behance import BehanceAdapter

    adapter = BehanceAdapter()
    url = sources.checked_url("https://www.behance.net/gallery/123/Public-Project")
    matched = adapter.match(url)
    body = (
        b'{"type":"rich","title":"Public Project - Ada Lovelace",'
        b'"author_name":"Ada Lovelace",'
        b'"author_url":"https://www.behance.net/ada",'
        b'"description":"<p>Uma interface para equipes.</p><script>bad()</script>",'
        b'"html":"<iframe src=\\"https://www.behance.net/embed/project/123\\"></iframe>",'
        b'"thumbnail_url":"https://mir-s3-cdn-cf.behance.net/projects/max_808/cover.png",'
        b'"video_url":"https://cdn-prod-ccv.adobe.com/clip.mp4",'
        b'"author_avatar_url":"https://pps.services.adobe.com/avatar.jpg",'
        b'"image":"http://127.0.0.1/private.png"}'
    )
    result = adapter.parse(body, url, matched)
    previews = adapter.media(body, url, matched)
    assert result.content_kind == "metadata" and result.native_title is False
    assert result.title == "Ada Lovelace" and result.author_handle == "ada"
    assert result.text == "Uma interface para equipes."
    assert "script" not in result.text and "iframe" not in result.text
    assert "127.0.0.1" not in result.text
    assert previews == [
        {"url": "https://pps.services.adobe.com/avatar.jpg", "kind": "avatar"},
        {
            "url": "https://mir-s3-cdn-cf.behance.net/projects/max_808/cover.png",
            "kind": "video",
            "clip_url": "https://cdn-prod-ccv.adobe.com/clip.mp4",
        },
    ]


def test_behance_store_keeps_module_order_and_first_clip_only():
    from queep.social.behance import BehanceAdapter

    adapter = BehanceAdapter()
    url = sources.checked_url("https://www.behance.net/gallery/123/Mixer")
    matched = adapter.match(url)
    cdn = "https://mir-s3-cdn-cf.behance.net/project_modules"

    def image(name, size="1400"):
        return {
            "__typename": "ImageModule",
            "imageSizes": {"allAvailable": [
                {"url": f"{cdn}/disp/{name}"},
                {"url": f"{cdn}/{size}/{name}"},
            ]},
        }

    body = json.dumps({
        "project": {"project": {
            "name": "Mixer",
            "description": "<p>Tres pecas do estudo.</p><script>bad()</script>",
            "owners": [{
                "displayName": "Ada Lovelace",
                "username": "ada",
                "url": "https://www.behance.net/ada",
                "images": {"100": "https://pps.services.adobe.com/avatar.jpg"},
            }],
            "modules": [
                image("one.png"),
                {"__typename": "ImageModule", "src": "http://127.0.0.1/private.png"},
                {
                    "__typename": "VideoModule",
                    "videoUrl": "https://cdn-prod-ccv.adobe.com/first.mp4",
                    "imageSizes": {"allAvailable": [
                        {"url": f"{cdn}/1400/poster.png"},
                    ]},
                },
                {
                    "__typename": "VideoModule",
                    "videoUrl": "https://cdn-prod-ccv.adobe.com/second.mp4",
                    "imageSizes": {"allAvailable": [
                        {"url": f"{cdn}/1400/later.png"},
                    ]},
                },
                image("four.png"),
                image("five.png"),
            ],
        }},
    }).encode()
    source = adapter.parse(body, url, matched)
    media = adapter.media(body, url, matched)
    assert source.author_handle == "ada" and "estudo" in source.text
    assert "script" not in source.text and source.native_title is False
    slides = [item for item in media if item["kind"] != "avatar"]
    assert [item["url"] for item in slides] == [
        f"{cdn}/1400/one.png",
        f"{cdn}/1400/poster.png",
        f"{cdn}/1400/later.png",
        f"{cdn}/1400/four.png",
    ]
    assert slides[1]["clip_url"].endswith("first.mp4")
    assert "clip_url" not in slides[2]
    assert media[0] == {"url": "https://pps.services.adobe.com/avatar.jpg", "kind": "avatar"}
    assert "127.0.0.1" not in str(media)


def test_behance_html_reads_cover_modules_and_adobe_avatar():
    from queep.social.behance import BehanceAdapter

    adapter = BehanceAdapter()
    url = sources.checked_url("https://www.behance.net/gallery/123/Public-Project")
    matched = adapter.match(url)
    body = b"""<html><head>
        <title>Public Project :: Behance</title>
        <meta property="og:title" content="Public Project - Ada Lovelace">
        <meta property="og:owners" content="Ada Lovelace">
        <meta property="og:description" content="Descricao publica do projeto.">
        <meta property="og:image" content="https://mir-s3-cdn-cf.behance.net/projects/max_808/cover.png">
        <meta property="og:image" content="http://127.0.0.1/private.png">
        <meta property="og:video" content="https://cdn-prod-ccv.adobe.com/clip.mp4">
        </head><body>
        <a href="https://www.behance.net/ada">Ada Lovelace</a>
        <img src="https://pps.services.adobe.com/avatar.jpg" alt="Ada Lovelace's profile">
        <img src="https://mir-s3-cdn-cf.behance.net/project_modules/1400/one.png">
        <img src="https://mir-s3-cdn-cf.behance.net/project_modules/disp/one.png">
        <img src="https://a5.behance.net/img/project/tools/after_effects.png">
        <script>private()</script>
        </body></html>"""
    result = adapter.parse(body, url, matched)
    previews = adapter.media(body, url, matched)
    assert result.author_name == "Ada Lovelace" and result.author_handle == "ada"
    assert result.text == "Descricao publica do projeto."
    assert previews == [
        {"url": "https://pps.services.adobe.com/avatar.jpg", "kind": "avatar"},
        {
            "url": "https://mir-s3-cdn-cf.behance.net/projects/max_808/cover.png",
            "kind": "video",
            "clip_url": "https://cdn-prod-ccv.adobe.com/clip.mp4",
        },
        {"url": "https://mir-s3-cdn-cf.behance.net/project_modules/1400/one.png", "kind": "photo"},
    ]


def test_behance_profile_keeps_avatar_out_of_project_gallery_and_allows_empty_bio():
    from queep.social.behance import BehanceAdapter

    adapter = BehanceAdapter()
    url = sources.checked_url("https://www.behance.net/ada")
    matched = adapter.match(url)
    body = b"""<html><head>
        <title>Ada Lovelace | Behance</title>
        <meta property="og:title" content="Ada Lovelace - Designer in Sao Paulo">
        <meta property="og:url" content="https://www.behance.net/ada">
        <meta property="og:description" content="">
        <meta property="og:image" content="https://mir-s3-cdn-cf.behance.net/banner.png">
        </head><body>
        <img src="https://pps.services.adobe.com/avatar.jpg" alt="Ada Lovelace's profile">
        <img src="https://mir-s3-cdn-cf.behance.net/projects/404/cover.png">
        </body></html>"""
    result = adapter.parse(body, url, matched)
    assert result.content_kind == "profile" and result.native_title is True
    assert result.title == "Ada Lovelace" and result.author_handle == "ada"
    assert result.text == ""
    assert adapter.media(body, url, matched) == [
        {"url": "https://pps.services.adobe.com/avatar.jpg", "kind": "avatar"}
    ]


@pytest.mark.parametrize(
    "url,role,key",
    [
        ("https://www.pinterest.com/pin/545287467382811935/", "post", "545287467382811935"),
        (
            "https://br.pinterest.com/pin/pinterest-design--545287467382811935/",
            "post",
            "545287467382811935",
        ),
        ("https://pin.it/4Ab_Cd9", "post", "4Ab_Cd9"),
        ("https://www.pinterest.com/biztechprenista/", "profile", "biztechprenista"),
    ],
)
def test_pinterest_matches_pins_and_creator_profiles(url, role, key):
    from queep.social.pinterest import PinterestAdapter

    matched = PinterestAdapter().match(sources.checked_url(url))
    assert matched.network == "pinterest" and matched.role == role and matched.key == key


@pytest.mark.parametrize(
    "url",
    [
        "https://www.pinterest.com/pin/",
        "https://www.pinterest.com/pin/not-a-pin/",
        "https://www.pinterest.com/search/",
        "https://www.pinterest.com/today/",
        "https://www.pinterest.com/123456/",
        "https://www.pinterest.com/ada/boards/",
        "https://www.pinterest.com.example.org/pin/545287467382811935/",
        "https://pin.it/",
        "https://pin.it/code/extra",
    ],
)
def test_pinterest_rejects_reserved_noncanonical_and_spoofed_urls(url):
    from queep.social.pinterest import PinterestAdapter

    assert PinterestAdapter().match(sources.checked_url(url)) is None


def test_pinterest_pin_readers_prefer_public_html_then_official_oembed():
    from queep.social.pinterest import PinterestAdapter

    adapter = PinterestAdapter()
    url = sources.checked_url(
        "https://br.pinterest.com/pin/design--545287467382811935/?utm_source=queep"
    )
    readers = adapter.readers(url, adapter.match(url))
    assert readers == [
        "https://www.pinterest.com/pin/545287467382811935/",
        "https://www.pinterest.com/oembed.json?url="
        "https%3A%2F%2Fwww.pinterest.com%2Fpin%2F545287467382811935%2F",
    ]


def test_pinterest_html_reads_video_pin_author_caption_avatar_and_public_media():
    from queep.social.pinterest import PinterestAdapter

    adapter = PinterestAdapter()
    url = sources.checked_url("https://www.pinterest.com/pin/324188873194809196/")
    matched = adapter.match(url)
    body = b"""<html><head>
      <meta property="og:image" content="https://i.pinimg.com/736x/fallback.jpg">
      <meta property="og:description" content="Fallback caption">
      </head><body>
      <script type="application/ld+json">{
        "@type":"SocialMediaPosting",
        "author":{"@type":"Person","name":"Ada Lovelace","alternateName":"ada_design","url":"https://www.pinterest.com/ada_design/"},
        "headline":"Motion study",
        "articleBody":"Uma interface em movimento.",
        "image":"http://127.0.0.1/private.jpg",
        "interactionStatistic":[{"userInteractionCount":999999}]
      }</script>
      <script type="application/ld+json">{
        "@type":"VideoObject",
        "name":"Motion study",
        "thumbnailUrl":"https://i.pinimg.com/videos/thumbnails/originals/poster.jpg",
        "contentUrl":"https://v1.pinimg.com/videos/iht/720p/clip.mp4",
        "commentCount":1234
      }</script>
      <img alt="ada_design" src="https://i.pinimg.com/75x75_RS/avatar.jpg">
      <script>private()</script>
      </body></html>"""
    result = adapter.parse(body, url, matched)
    previews = adapter.media(body, url, matched)
    assert result.content_kind == "metadata" and result.native_title is False
    assert result.title == "Ada Lovelace" and result.author_handle == "ada_design"
    assert result.text == "Uma interface em movimento."
    assert "999999" not in result.text and "1234" not in result.text
    assert previews == [
        {"url": "https://i.pinimg.com/75x75_RS/avatar.jpg", "kind": "avatar"},
        {
            "url": "https://i.pinimg.com/videos/thumbnails/originals/poster.jpg",
            "kind": "video",
            "clip_url": "https://v1.pinimg.com/videos/iht/720p/clip.mp4",
        },
    ]
    assert "127.0.0.1" not in str(previews)


def test_pinterest_profile_reads_public_json_ld_bio_and_avatar_only():
    from queep.social.pinterest import PinterestAdapter

    adapter = PinterestAdapter()
    url = sources.checked_url("https://www.pinterest.com/ada_design/")
    matched = adapter.match(url)
    body = b"""<html><head>
      <meta property="og:image" content="https://s.pinimg.com/images/default_open_graph_1200.png">
      </head><body><script type="application/ld+json">{
        "@type":"ProfilePage",
        "mainEntity":{"@type":"Person","name":"Ada Lovelace","alternateName":"ada_design",
          "url":"https://www.pinterest.com/ada_design/",
          "description":"Design de produtos digitais.",
          "image":{"@type":"ImageObject","contentUrl":"https://i.pinimg.com/280x280_RS/avatar.jpg"}}
      }</script></body></html>"""
    result = adapter.parse(body, url, matched)
    assert result.content_kind == "profile" and result.native_title is True
    assert result.title == "Ada Lovelace" and result.author_handle == "ada_design"
    assert result.text == "Design de produtos digitais."
    assert adapter.media(body, url, matched) == [
        {"url": "https://i.pinimg.com/280x280_RS/avatar.jpg", "kind": "avatar"}
    ]


def test_pinterest_oembed_is_a_safe_metadata_fallback():
    from queep.social.pinterest import PinterestAdapter

    adapter = PinterestAdapter()
    url = sources.checked_url("https://www.pinterest.com/pin/545287467382811935/")
    matched = adapter.match(url)
    body = br"""{
      "provider_name":"Pinterest","type":"rich","title":"Uma ideia para o escritorio",
      "author_name":"Ada Lovelace","author_url":"https://www.pinterest.com/ada_design/",
      "thumbnail_url":"https://i.pinimg.com/236x/public.jpg",
      "html":"<iframe src=\"https://assets.pinterest.com/ext/embed.html?id=1\"></iframe>"
    }"""
    result = adapter.parse(body, url, matched)
    assert result.title == "Ada Lovelace" and result.author_handle == "ada_design"
    assert result.text == "Uma ideia para o escritorio"
    assert "iframe" not in result.text
    assert adapter.media(body, url, matched) == [
        {"url": "https://i.pinimg.com/236x/public.jpg", "kind": "photo"}
    ]


def test_pinterest_generic_javascript_shell_defers_to_oembed():
    from queep.social.pinterest import PinterestAdapter

    adapter = PinterestAdapter()
    url = sources.checked_url("https://www.pinterest.com/pin/545287467382811935/")
    matched = adapter.match(url)
    body = b"<html><head><title>Pinterest</title></head><body>Enable JavaScript</body></html>"
    with pytest.raises(sources.SourceError, match="empty"):
        adapter.parse(body, url, matched)
    assert adapter.media(body, url, matched) == []


@pytest.mark.parametrize(
    "url,tweet_id",
    [
        ("https://x.com/richardrx/status/2099853605054316775", "2099853605054316775"),
        ("https://twitter.com/richardrx/status/2099853605054316775/photo/1", "2099853605054316775"),
        ("https://mobile.twitter.com/i/web/status/2099853605054316775", "2099853605054316775"),
    ],
)
def test_tweet_endpoint_contains_only_a_canonical_status_id(url, tweet_id):
    parsed = sources.checked_url(url)
    assert sources.tweet_status_id(parsed) == tweet_id
    endpoint = sources.social_endpoint(parsed)
    assert endpoint == "https://api.fxtwitter.com/status/" + tweet_id
    readers = sources.tweet_readers(parsed)
    assert readers[0].endswith("/" + tweet_id)
    assert "publish.x.com/oembed" in readers[1] and tweet_id in readers[1]
    assert (
        sources.tweet_status_id(sources.checked_url("https://x.com.example.org/status/" + tweet_id))
        is None
    )


def test_tweet_metadata_keeps_subject_text_and_drops_media():
    body = (
        b'{"code":200,"tweet":{"text":"Churn em D60, mas o first value ja tinha falhado.",'
        b'"author":{"name":"Richard - Design for startups","screen_name":"richardrx"},'
        b'"media":{"photos":[{"url":"http://127.0.0.1/private.jpg"}]}}}'
    )
    result = sources.tweet_metadata(body, sources.checked_url("https://x.com/a/status/2099853605054316775"))
    assert result.content_kind == "metadata" and result.state == "read"
    assert result.title.startswith("Richard")
    assert "first value" in result.text and "D60" in result.text
    assert "127.0.0.1" not in result.text and "private.jpg" not in result.text
    assert "Twitter" not in result.text and "Views" not in result.text
    assert "Respostas não foram analisadas" in result.limitations[0]
    assert result.author_handle == "richardrx"
    assert result.native_title is False
    assert sources.tweet_photo_url(body) is None


def test_tweet_photo_url_keeps_only_public_cdn():
    body = (
        b'{"tweet":{"text":"Hello there world ok ok.","author":{"name":"Ada"},'
        b'"media":{"photos":[{"url":"https://pbs.twimg.com/media/abc.jpg?name=orig"}]}}}'
    )
    assert sources.tweet_photo_url(body) == "https://pbs.twimg.com/media/abc.jpg?name=orig"


def test_tweet_photo_urls_keep_carousel_order_and_skip_private_hosts():
    body = (
        b'{"tweet":{"text":"Hello there world ok ok.","author":{"name":"Ada"},'
        b'"media":{"photos":['
        b'{"url":"https://pbs.twimg.com/media/one.jpg?name=orig"},'
        b'{"url":"http://127.0.0.1/private.jpg"},'
        b'{"url":"https://pbs.twimg.com/media/two.jpg?name=orig"},'
        b'{"url":"https://pbs.twimg.com/media/one.jpg?name=orig"},'
        b'{"url":"https://pbs.twimg.com/media/three.jpg?name=orig"}'
        b']}}}'
    )
    assert sources.tweet_photo_urls(body) == [
        "https://pbs.twimg.com/media/one.jpg?name=orig",
        "https://pbs.twimg.com/media/two.jpg?name=orig",
        "https://pbs.twimg.com/media/three.jpg?name=orig",
    ]
    assert sources.tweet_photo_url(body) == "https://pbs.twimg.com/media/one.jpg?name=orig"


def test_fetch_keeps_tweet_photos_when_metadata_is_incomplete(monkeypatch):
    body = (
        b'{"tweet":{"text":"Hi","author":{},'
        b'"media":{"photos":[{"url":"https://pbs.twimg.com/media/one.jpg?name=orig"},'
        b'{"url":"https://pbs.twimg.com/media/two.jpg?name=orig"}]}}}'
    )

    async def fake_fetch_resolved_body(_url):
        return body, {}, _url

    monkeypatch.setattr(sources, "fetch_resolved_body", fake_fetch_resolved_body)

    async def run():
        return await sources.fetch("https://x.com/a/status/2099849967774863489")

    source, photos = asyncio.run(run())
    assert source.state == "read"
    assert source.title == "a"
    assert photos == [
        {"url": "https://pbs.twimg.com/media/one.jpg?name=orig", "kind": "photo"},
        {"url": "https://pbs.twimg.com/media/two.jpg?name=orig", "kind": "photo"},
    ]


def test_fetch_prepends_public_author_avatar(monkeypatch):
    body = (
        b'{"tweet":{"text":"Hello there world ok ok.","author":{"name":"Ada","screen_name":"ada",'
        b'"avatar_url":"https://pbs.twimg.com/profile_images/ada.jpg"},'
        b'"media":{"photos":[{"url":"https://pbs.twimg.com/media/one.jpg?name=orig"}]}}}'
    )

    async def fake_fetch_resolved_body(_url):
        return body, {}, _url

    monkeypatch.setattr(sources, "fetch_resolved_body", fake_fetch_resolved_body)

    async def run():
        return await sources.fetch("https://x.com/ada/status/2099849967774863489")

    source, previews = asyncio.run(run())
    assert source.author_handle == "ada"
    assert previews[0] == {"url": "https://pbs.twimg.com/profile_images/ada.jpg", "kind": "avatar"}
    assert previews[1]["kind"] == "photo"


def test_tweet_photo_urls_complete_carousel_from_mosaic_when_photos_truncated():
    body = (
        b'{"tweet":{"text":"Hello there world ok ok.","author":{"name":"Ada"},'
        b'"media":{"photos":[{"url":"https://pbs.twimg.com/media/HSQNkRHagAAA1kO.jpg?name=orig"}],'
        b'"mosaic":{"type":"mosaic_photo","formats":{"jpeg":'
        b'"https://mosaic.fxtwitter.com/jpeg/2099849967774863489/HSQNkRHagAAA1kO/HSQNk3zbgAA4oaJ"}}}}}'
    )
    assert sources.tweet_photo_urls(body) == [
        "https://pbs.twimg.com/media/HSQNkRHagAAA1kO.jpg?name=orig",
        "https://pbs.twimg.com/media/HSQNk3zbgAA4oaJ.jpg?name=orig",
    ]


def test_tweet_photo_urls_read_media_all_when_photos_is_missing():
    body = (
        b'{"tweet":{"text":"Hello there world ok ok.","author":{"name":"Ada"},'
        b'"media":{"all":['
        b'{"type":"photo","url":"https://pbs.twimg.com/media/one.jpg?name=orig"},'
        b'{"type":"video","url":"https://pbs.twimg.com/media/clip.mp4"},'
        b'{"type":"photo","url":"https://pbs.twimg.com/media/two.jpg?name=orig"}'
        b']}}}'
    )
    assert sources.tweet_photo_urls(body) == [
        "https://pbs.twimg.com/media/one.jpg?name=orig",
        "https://pbs.twimg.com/media/two.jpg?name=orig",
    ]


def test_tweet_media_previews_keep_video_posters_in_carousel_order():
    body = (
        b'{"tweet":{"text":"Hello there world ok ok.","author":{"name":"Ada"},'
        b'"media":{"all":['
        b'{"type":"photo","url":"https://pbs.twimg.com/media/one.jpg?name=orig"},'
        b'{"type":"video","url":"https://video.twimg.com/amplify_video/clip.mp4",'
        b'"thumbnail_url":"https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg"},'
        b'{"type":"gif","thumbnail_url":"https://pbs.twimg.com/tweet_video_thumb/loop.jpg"},'
        b'{"type":"photo","url":"https://pbs.twimg.com/media/two.jpg?name=orig"}'
        b']}}}'
    )
    assert sources.tweet_media_previews(body) == [
        {"url": "https://pbs.twimg.com/media/one.jpg?name=orig", "kind": "photo"},
        {"url": "https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg", "kind": "video",
         "clip_url": "https://video.twimg.com/amplify_video/clip.mp4"},
        {"url": "https://pbs.twimg.com/tweet_video_thumb/loop.jpg", "kind": "photo"},
        {"url": "https://pbs.twimg.com/media/two.jpg?name=orig", "kind": "photo"},
    ]
    assert all("video.twimg.com" not in item["url"] for item in sources.tweet_media_previews(body))


def test_tweet_media_previews_read_videos_array_without_all():
    body = (
        b'{"tweet":{"text":"Watch this.","author":{"name":"Ada"},'
        b'"media":{"videos":[{'
        b'"url":"https://video.twimg.com/amplify_video/clip.mp4",'
        b'"thumbnail_url":"https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg"'
        b'}]}}}'
    )
    assert sources.tweet_media_previews(body) == [
        {"url": "https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg", "kind": "video",
         "clip_url": "https://video.twimg.com/amplify_video/clip.mp4"},
    ]


def test_fetch_keeps_tweet_video_posters_when_metadata_is_complete(monkeypatch):
    body = (
        b'{"tweet":{"text":"Hello there world ok ok.","author":{"name":"Ada","screen_name":"ada"},'
        b'"media":{"videos":[{'
        b'"type":"video",'
        b'"url":"https://video.twimg.com/amplify_video/clip.mp4",'
        b'"thumbnail_url":"https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg"'
        b'}]}}}'
    )

    async def fake_fetch_resolved_body(_url):
        return body, {}, _url

    monkeypatch.setattr(sources, "fetch_resolved_body", fake_fetch_resolved_body)

    async def run():
        return await sources.fetch("https://x.com/ada/status/2099849967774863489")

    source, previews = asyncio.run(run())
    assert source.title == "Ada"
    assert source.author_name == "Ada"
    assert source.author_handle == "ada"
    assert source.native_title is False
    assert previews == [
        {"url": "https://pbs.twimg.com/ext_tw_video_thumb/poster.jpg", "kind": "video",
         "clip_url": "https://video.twimg.com/amplify_video/clip.mp4"},
    ]


def test_clean_tweet_text_drops_media_suffixes_and_keeps_breaks():
    raw = "Primeira linha\n\nSegunda linha\nhttps://t.co/abc123\npic.x.com/zzzz"
    assert sources.clean_tweet_text(raw) == "Primeira linha\n\nSegunda linha"


def test_tweet_oembed_fallback_reads_paragraph_not_embed_html():
    body = (
        b'{"author_name":"Richard - Design for startups","html":'
        b'"<blockquote class=\\"twitter-tweet\\"><p>Teu usuario some do produto.'
        b'</p><script>bad()</script></blockquote>"}'
    )
    result = sources.tweet_metadata(body, sources.checked_url("https://x.com/a/status/2099853605054316775"))
    assert "Teu usuario some" in result.text
    assert "bad" not in result.text and "<script>" not in result.text


def test_unreadable_tweet_payload_is_not_fabricated():
    with pytest.raises(sources.SourceError, match="empty"):
        sources.tweet_metadata(b'{"code":404}', sources.checked_url("https://x.com/a/status/12345"))


def test_tweet_keeps_author_when_caption_is_missing():
    body = (
        b'{"tweet":{"text":"","author":{"name":"Yury","screen_name":"yurygok"}}}'
    )
    result = sources.tweet_metadata(
        body, sources.checked_url("https://x.com/yurygok/status/2100117746117673043")
    )
    assert result.title == "Yury" and result.text == ""
    assert result.author_name == "Yury" and result.author_handle == "yurygok"
    assert result.native_title is False


def test_tweet_photo_fallback_uses_handle_from_status_url():
    result = sources.tweet_photo_fallback(
        "https://x.com/yurygok/status/2100117746117673043",
        ["https://pbs.twimg.com/media/mixer.jpg"],
    )
    assert result.title == "yurygok"
    assert result.author_handle == "yurygok"
    assert result.native_title is False
    assert sources.tweet_handle_from_url("https://x.com/i/web/status/1") is None


def test_tweet_fetch_falls_back_to_official_oembed_when_full_text_fails(monkeypatch):
    seen = []

    async def resolve(host):
        seen.append(host)
        return ["93.184.215.14"]

    async def request(url, address):
        seen.append(str(url))
        if "fxtwitter.com" in str(url):
            raise sources.SourceError("http")
        return 200, {"content-type": "application/json"}, (
            b'{"author_name":"Richard","html":"<blockquote><p>Churn no D60</p></blockquote>"}'
        )

    monkeypatch.setattr(sources, "resolve", resolve)
    monkeypatch.setattr(sources, "request", request)
    result = sources.read_source("https://x.com/richardrx/status/2099853605054316775")
    assert result.state == "read" and "D60" in result.text
    assert any("fxtwitter.com" in item for item in seen)
    assert any("publish.x.com" in item for item in seen)


def test_tweet_media_previews_keep_clip_url_only_on_first_video():
    body = (
        b'{"tweet":{"text":"Dois clips.","author":{"name":"Ada"},'
        b'"media":{"all":['
        b'{"type":"video","url":"https://video.twimg.com/amplify_video/one.mp4",'
        b'"thumbnail_url":"https://pbs.twimg.com/ext_tw_video_thumb/one.jpg"},'
        b'{"type":"video","url":"https://video.twimg.com/amplify_video/two.mp4",'
        b'"thumbnail_url":"https://pbs.twimg.com/ext_tw_video_thumb/two.jpg"}'
        b']}}}'
    )
    previews = sources.tweet_media_previews(body)
    assert previews[0]["clip_url"] == "https://video.twimg.com/amplify_video/one.mp4"
    assert "clip_url" not in previews[1]
    assert previews[1]["kind"] == "video"


@pytest.mark.parametrize(
    "url,handle",
    [
        ("https://x.com/ada", "ada"),
        ("https://twitter.com/Ada_123/", "Ada_123"),
        ("https://mobile.twitter.com/jack", "jack"),
    ],
)
def test_twitter_profile_handle_from_canonical_path(url, handle):
    parsed = sources.checked_url(url)
    assert sources.twitter_profile_handle(parsed) == handle
    assert sources.tweet_status_id(parsed) is None
    assert sources.profile_readers(parsed) == [
        "https://api.fxtwitter.com/2/profile/" + handle
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/ada/status/1",
        "https://x.com/i/web/status/1",
        "https://x.com/search?q=ada",
        "https://x.com/hashtag/design",
        "https://x.com/intent/user?screen_name=ada",
        "https://x.com/ada/with_replies",
        "https://example.com/ada",
    ],
)
def test_twitter_profile_handle_rejects_posts_and_reserved_paths(url):
    parsed = sources.checked_url(url)
    assert sources.twitter_profile_handle(parsed) is None
    assert sources.profile_readers(parsed) == []


def test_profile_metadata_keeps_bio_and_drops_banner():
    body = (
        b'{"code":200,"user":{"name":"Ada Lovelace","screen_name":"ada",'
        b'"description":"Notas sobre computacao.",'
        b'"avatar_url":"https://pbs.twimg.com/profile_images/ada_normal.jpg",'
        b'"banner_url":"https://pbs.twimg.com/profile_banners/1/banner.jpg",'
        b'"followers":99}}'
    )
    original = sources.checked_url("https://x.com/ada")
    result = sources.profile_metadata(body, original)
    assert result.content_kind == "profile"
    assert result.author_name == "Ada Lovelace"
    assert result.author_handle == "ada"
    assert result.text == "Notas sobre computacao."
    assert result.native_title is True
    assert "banner" not in result.text and "99" not in result.text
    assert sources.profile_avatar_url(body) == (
        "https://pbs.twimg.com/profile_images/ada_400x400.jpg"
    )


def test_profile_metadata_allows_empty_bio():
    body = b'{"user":{"name":"Ada","screen_name":"ada","description":""}}'
    result = sources.profile_metadata(body, sources.checked_url("https://x.com/ada"))
    assert result.text == ""
    assert result.author_handle == "ada"


def test_profile_avatar_url_skips_private_hosts():
    body = b'{"user":{"avatar_url":"http://127.0.0.1/avatar.jpg"}}'
    assert sources.profile_avatar_url(body) is None


def test_fetch_reads_profile_endpoint_not_tweet(monkeypatch):
    seen = []
    body = (
        b'{"user":{"name":"Ada","screen_name":"ada","description":"Bio curta.",'
        b'"avatar_url":"https://pbs.twimg.com/profile_images/ada.jpg"}}'
    )

    async def fake_fetch_resolved_body(url):
        seen.append(str(url))
        return body, {}, url

    monkeypatch.setattr(sources, "fetch_resolved_body", fake_fetch_resolved_body)
    source, previews = asyncio.run(sources.fetch("https://x.com/ada"))
    assert source.content_kind == "profile"
    assert source.text == "Bio curta."
    assert seen == ["https://api.fxtwitter.com/2/profile/ada"]
    assert previews == [
        {"url": "https://pbs.twimg.com/profile_images/ada.jpg", "kind": "avatar"}
    ]


def test_extract_uses_site_name_not_tab_title():
    page = (
        b"<html><head><title>Linear - The issue tracking tool you will enjoy using</title>"
        b'<meta property="og:site_name" content="Linear">'
        b'<meta property="og:image" content="https://linear.app/og.png">'
        b'<link rel="icon" type="image/png" href="/icon.png">'
        b"</head><body><main><p>" + b"Issue tracking for software teams. " * 8
        + b"</p></main></body></html>"
    )
    result = sources.extract(page, {"content-type": "text/html"}, "https://linear.app")
    assert result.content_kind == "page"
    assert result.title == "Linear"
    assert result.native_title is False
    assert "Issue tracking" in result.text
    assert sources.page_previews(page, "https://linear.app") == [
        {"url": "https://linear.app/og.png", "kind": "photo"},
        {"url": "https://linear.app/icon.png", "kind": "avatar"},
    ]


def test_page_previews_skip_private_hosts_and_svg():
    page = (
        b"<html><head>"
        b'<meta property="og:image" content="http://127.0.0.1/og.png">'
        b'<link rel="icon" type="image/svg+xml" href="/mark.svg">'
        b'<link rel="apple-touch-icon" href="https://cdn.linear.app/icon.png">'
        b"</head></html>"
    )
    assert sources.page_previews(page, "https://linear.app") == [
        {"url": "https://cdn.linear.app/icon.png", "kind": "avatar"},
    ]


def test_fetch_returns_site_favicon_and_og_image(monkeypatch):
    page = (
        b"<html><head><title>Cursor</title>"
        b'<meta property="og:site_name" content="Cursor">'
        b'<meta property="og:image" content="https://cursor.com/og.png">'
        b'<link rel="icon" href="/favicon.png">'
        b"</head><body><main><p>" + b"AI code editor for software teams. " * 8
        + b"</p></main></body></html>"
    )

    async def fake_fetch_resolved_body(url):
        return page, {"content-type": "text/html"}, url

    monkeypatch.setattr(sources, "fetch_resolved_body", fake_fetch_resolved_body)
    source, previews = asyncio.run(sources.fetch("https://cursor.com"))
    assert source.title == "Cursor" and source.native_title is False
    assert previews == [
        {"url": "https://cursor.com/og.png", "kind": "photo"},
        {"url": "https://cursor.com/favicon.png", "kind": "avatar"},
    ]


def test_script_shell_keeps_favicon_and_og_image(monkeypatch):
    page = (
        b"<html><head><title>Fontshare</title>"
        b'<meta property="og:site_name" content="Fontshare">'
        b'<meta property="og:description" content="Free fonts from the Indian Type Foundry.">'
        b'<meta property="og:image" content="https://www.fontshare.com/share.jpg">'
        b'<link rel="icon" type="image/png" href="/static/favicon.png">'
        b"</head><body><main><p>Loading</p></main></body></html>"
    )

    async def fake_fetch_resolved_body(url):
        return page, {"content-type": "text/html"}, url

    monkeypatch.setattr(sources, "fetch_resolved_body", fake_fetch_resolved_body)
    source, previews = asyncio.run(sources.fetch("https://www.fontshare.com/"))
    assert source.title == "Fontshare" and source.state == "read"
    assert "Indian Type Foundry" in source.text
    assert previews == [
        {"url": "https://www.fontshare.com/share.jpg", "kind": "photo"},
        {"url": "https://www.fontshare.com/static/favicon.png", "kind": "avatar"},
    ]


def test_svg_icon_falls_back_to_favicon_file():
    page = (
        b"<html><head><title>Hacker News</title>"
        b'<link rel="icon" href="y18.svg">'
        b"</head><body><main><p>" + b"News for hackers. " * 8 + b"</p></main></body></html>"
    )
    assert sources.page_previews(page, "https://news.ycombinator.com") == [
        {"url": "https://news.ycombinator.com/favicon.ico", "kind": "avatar"},
    ]


def test_page_without_picture_keeps_a_public_screenshot():
    url = sources.checked_url("https://news.ycombinator.com/")
    bare = [{"url": "https://news.ycombinator.com/favicon.ico", "kind": "avatar"}]
    assert sources.with_page_shot(url, bare) == [
        *bare,
        {"url": "https://r.jina.ai/https://news.ycombinator.com/", "kind": "photo"},
    ]
    pictured = [{"url": "https://cdn.linear.app/poster.jpg", "kind": "photo"}]
    assert sources.with_page_shot(url, pictured) == pictured


def test_page_video_keeps_poster_and_clip():
    page = (
        b"<html><head>"
        b'<meta property="og:image" content="https://cdn.linear.app/poster.jpg">'
        b'<meta property="og:video" content="https://cdn.linear.app/clip.mp4">'
        b'<link rel="icon" type="image/png" href="/favicon.png">'
        b"</head></html>"
    )
    assert sources.page_previews(page, "https://cdn.linear.app") == [
        {"url": "https://cdn.linear.app/poster.jpg", "kind": "video",
         "clip_url": "https://cdn.linear.app/clip.mp4"},
        {"url": "https://cdn.linear.app/favicon.png", "kind": "avatar"},
    ]


def test_markdown_page_keeps_title_images_and_favicon():
    body = (
        b'---\ntitle: "MotionSites"\nmeta:\n  description: "Prompts for beautiful websites '
        b'that ship with a landing page, a hero and a gallery of examples ready to paste."\n---\n'
        b'[![MotionSites logo](https://motionsites.ai/logo.png)](/)\n'
        b'![Plume](https://motionsites.ai/plume.jpg)\n'
    )
    result = sources.extract(body, {"content-type": "text/markdown"}, "https://motionsites.ai/")
    assert result.title == "MotionSites" and "landing page" in result.text
    assert sources.markdown_previews(body, "https://motionsites.ai/") == [
        {"url": "https://motionsites.ai/plume.jpg", "kind": "photo"},
        {"url": "https://motionsites.ai/favicon.ico", "kind": "avatar"},
    ]


def test_instagram_oembed_reads_author_caption_and_reel_poster():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    url = sources.checked_url("https://www.instagram.com/reel/DW7AKrtjQQl/")
    match = adapter.match(url)
    assert adapter.readers(url, match)[0].startswith("https://i.instagram.com/api/v1/oembed/")
    body = json.dumps({
        "provider_name": "Instagram",
        "author_name": "nasa",
        "author_url": "https://www.instagram.com/nasa",
        "title": "Solar eclipse in space from the Artemis II flyby.",
        "thumbnail_url": "https://instagram.fbsb13-1.fna.fbcdn.net/poster.jpg",
    }).encode()
    source = adapter.parse(body, url, match)
    media = adapter.media(body, url, match)
    assert source.author_handle == "nasa" and "eclipse" in source.text
    assert media[-1]["kind"] == "video"
    assert media[-1]["url"].endswith("poster.jpg")
    assert "clip_url" not in media[-1]


def test_instagram_without_media_still_points_at_the_public_poster():
    from queep.social.instagram import InstagramAdapter

    adapter = InstagramAdapter()
    url = sources.checked_url("https://www.instagram.com/p/AbC_12/")
    match = adapter.match(url)
    media = adapter.media(b"<html><title>Instagram</title></html>", url, match)
    assert media == [{
        "url": "https://www.instagram.com/p/AbC_12/media/?size=l",
        "kind": "photo",
    }]


def test_instagram_profile_html_yields_a_public_avatar():
    from queep.social.instagram import profile_pic_from_html

    body = (
        b'profile_pic_url":"https:\\/\\/scontent.cdninstagram.com\\/pic.jpg?a=1\\u0026b=2"'
    )
    assert profile_pic_from_html(body) == "https://scontent.cdninstagram.com/pic.jpg?a=1&b=2"
    assert profile_pic_from_html(b'profile_picture":null') is None


def test_public_image_never_fetches_private_hosts(monkeypatch):
    async def fail(*_):
        pytest.fail("A private image host reached the network")

    monkeypatch.setattr(sources, "request", fail)
    with pytest.raises(sources.SourceError, match="address"):
        asyncio.run(sources.fetch_public_image("http://127.0.0.1/favicon.png"))
