import asyncio
import gzip
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


def test_instagram_description_is_partial_not_a_transcript():
    result = sources.extract(
        b'<html><title>Post</title><meta property="og:description" '
        b'content="Three tips for building native applications."/></html>',
        {"content-type": "text/html"},
        "https://www.instagram.com/p/example/",
    )
    assert result.content_kind == "metadata"
    assert "Three tips" in result.text and "não foram analisados" in result.limitations[0]
