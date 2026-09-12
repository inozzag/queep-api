"""Bounded public text extraction. No browser, credentials, cookies or subresource fetches."""

import asyncio
import ipaddress
import json
import re
import socket
import zlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urljoin

import dns.asyncresolver
import dns.exception
import httpcore
import httpx
from bs4 import BeautifulSoup

from queep.models import SourceContent

MAX_BODY = 2 * 1024 * 1024
MAX_TEXT = 12000  # UTF-8 bytes, before any model-specific clipping.
TOTAL_TIMEOUT = 20
READ_LIMITATION = (
    "Foi lido apenas o texto acessível da página; imagens, áudio e vídeo não foram analisados."
)
MESSAGES = {
    "address": "Este endereço não permite leitura pública pelo Queep.",
    "timeout": "A página demorou para responder. O link e seu contexto foram preservados.",
    "network": "Não foi possível acessar a página nesta tentativa.",
    "http": "A página não está disponível publicamente ou recusou a leitura.",
    "transient": "A página está temporariamente indisponível. O link foi preservado.",
    "redirect": "A página redirecionou muitas vezes ou para um endereço não permitido.",
    "format": "Este formato ainda não permite leitura automática; o link foi preservado.",
    "size": "A página excedeu o limite de leitura automática.",
    "empty": "Não foi encontrado texto suficiente; a página pode exigir login ou JavaScript.",
}


class SourceError(Exception):
    pass


def clip(text, limit=MAX_TEXT):
    return text.encode()[:limit].decode("utf-8", errors="ignore")


def public_ip(value):
    ip = ipaddress.ip_address(value)
    # Disallow IPv6 translation/tunnel forms, including mapped private IPv4.
    return (
        ip.is_global
        and not ip.is_multicast
        and not ip.is_unspecified
        and not (
            ip.version == 6
            and (
                ip.ipv4_mapped
                or ip.sixtofour
                or ip.teredo
                or ip in ipaddress.ip_network("64:ff9b::/96")
            )
        )
    )


def checked_url(raw):
    try:
        if len(raw) > 4096 or re.search(r"[\x00-\x20\x7f\\]", raw):
            raise ValueError("url")
        url = httpx.URL(raw)
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.userinfo
            or url.port not in (None, 80 if url.scheme == "http" else 443)
        ):
            raise ValueError("url")
        host = url.raw_host.decode().rstrip(".").lower()
        if (
            "%" in host
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example"))
        ):
            raise ValueError("host")
        return url.copy_with(host=host, fragment=None)
    except (ValueError, httpx.InvalidURL) as error:
        raise SourceError("address") from error


async def resolve(host):
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        answer = await dns.asyncresolver.resolve_name(host, family=socket.AF_UNSPEC, lifetime=4)
        return list(answer.addresses())


async def destination(url):
    addresses = await resolve(url.raw_host.decode())
    if not addresses or not all(public_ip(value) for value in addresses):
        raise SourceError("address")
    # Prefer IPv4 where Cloud Run has no IPv6 egress. Never resolve the hostname again.
    return sorted(addresses, key=lambda value: ipaddress.ip_address(value).version)[0]


async def request(url, address):
    pinned = url.copy_with(host=address)
    headers = {
        "Host": url.netloc.decode(),
        "User-Agent": "Queep/0.1 (reference reader)",
        "Accept": "text/html,application/xhtml+xml,text/plain,text/markdown,application/json",
        "Accept-Encoding": "gzip, identity",
    }
    # HTTP core has no environment proxy/cookie jar. A fresh pool per hop prevents
    # sharing a connection across virtual hosts. TLS still verifies the original host.
    async with httpcore.AsyncConnectionPool(retries=0) as pool:
        async with pool.stream(
            "GET",
            str(pinned),
            headers=headers,
            extensions={
                "sni_hostname": url.raw_host.decode(),
                "timeout": {"connect": 5, "read": 5, "write": 5, "pool": 5},
            },
        ) as response:
            metadata = {
                key.decode().lower(): value.decode("latin-1") for key, value in response.headers
            }
            if response.status in (301, 302, 303, 307, 308):
                return response.status, metadata, b""
            if response.status == 429 or response.status >= 500:
                raise SourceError("transient")
            if response.status != 200:
                raise SourceError("http")
            kind = metadata.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind not in (
                "text/html",
                "application/xhtml+xml",
                "text/plain",
                "text/markdown",
                "application/json",
            ):
                raise SourceError("format")
            encoding = metadata.get("content-encoding", "identity").lower()
            if encoding not in ("identity", "gzip"):
                raise SourceError("format")
            length = metadata.get("content-length", "")
            if length.isdecimal() and int(length) > MAX_BODY:
                raise SourceError("size")
            body = bytearray()
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
            wire_bytes = 0
            async for chunk in response.aiter_stream():
                wire_bytes += len(chunk)
                if wire_bytes > MAX_BODY:
                    raise SourceError("size")
                if decoder:
                    try:
                        chunk = decoder.decompress(chunk, MAX_BODY + 1 - len(body))
                    except zlib.error as error:
                        raise SourceError("format") from error
                if len(body) + len(chunk) > MAX_BODY:
                    raise SourceError("size")
                body.extend(chunk)
            if decoder and (not decoder.eof or decoder.unused_data):
                raise SourceError("format")
            return response.status, metadata, bytes(body)


def extract(body, metadata, url):
    kind = metadata.get("content-type", "").split(";", 1)[0].lower()
    title = ""
    if kind == "application/json":
        raise SourceError("format")
    if kind in ("text/plain", "text/markdown"):
        text = body.decode("utf-8", errors="replace").strip()
    else:
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if re.search(
            r"just a moment|access denied|sign in|log in|attention required|"
            r"verify you are human|iniciar sess[aã]o",
            title,
            re.I,
        ):
            raise SourceError("empty")
        if httpx.URL(url).host in ("instagram.com", "www.instagram.com"):
            description = soup.find("meta", attrs={"property": "og:description"})
            if description and len(description.get("content", "")) >= 20:
                return SourceContent(
                    content_kind="metadata",
                    state="read",
                    url=str(url),
                    title=title[:200],
                    text=clip(description["content"], 3000),
                    fetched_at=datetime.now(UTC),
                    limitations=[
                        "Foi lida somente a descrição pública do post. Áudio, vídeo, imagens "
                        "e comentários não foram analisados. "
                        "Para analisar a mídia, envie o arquivo."
                    ],
                )
        for node in soup.select(
            "script,style,noscript,template,nav,header,footer,aside,"
            "form,button,svg,[hidden],[aria-hidden='true']"
        ):
            node.decompose()
        # GitHub renders public README content here; no code execution or repository crawl.
        content = soup.select_one("article.markdown-body") or soup.article or soup.main or soup.body
        if not content:
            raise SourceError("empty")
        text = "\n".join(
            line.strip() for line in content.get_text("\n", strip=True).splitlines() if line.strip()
        )
    if len(text) < 80:
        raise SourceError("empty")
    truncated = len(text.encode()) > MAX_TEXT
    limitations = [READ_LIMITATION]
    if truncated:
        limitations.append("A página é longa; somente um trecho foi armazenado e analisado.")
    return SourceContent(
        state="read",
        url=str(url),
        title=title[:200],
        text=clip(text),
        fetched_at=datetime.now(UTC),
        truncated=truncated,
        limitations=limitations,
    )


def social_endpoint(url):
    """Only canonical public video IDs; never pass arbitrary addresses to an oEmbed proxy."""
    video_id = None
    if url.host == "youtu.be":
        video_id = url.path.strip("/")
    elif url.host in ("youtube.com", "www.youtube.com", "m.youtube.com"):
        if url.path == "/watch":
            video_id = parse_qs(url.query.decode()).get("v", [""])[0]
        elif re.fullmatch(r"/(shorts|embed|live)/[^/]+/?", url.path):
            video_id = url.path.split("/")[2]
    if video_id and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        canonical = "https://www.youtube.com/watch?v=" + video_id
        return "https://www.youtube.com/oembed?" + urlencode({"url": canonical, "format": "json"})
    if url.host in ("vimeo.com", "www.vimeo.com") and re.fullmatch(r"/[0-9]{1,12}/?", url.path):
        return "https://vimeo.com/api/oembed.json?" + urlencode(
            {"url": "https://vimeo.com" + url.path}
        )
    return None


def video_metadata(body, original):
    try:
        value = json.loads(body)
        title, author = value.get("title", ""), value.get("author_name", "")
        if not isinstance(title, str) or not title.strip() or not isinstance(author, str):
            raise ValueError("metadata")
        return SourceContent(
            content_kind="metadata",
            state="read",
            url=str(original),
            title=title[:200],
            text=f"Título: {title[:1000]}\nAutor: {author[:200]}",
            fetched_at=datetime.now(UTC),
            limitations=[
                "Somente título e autor públicos foram lidos. A fala e as cenas do vídeo "
                "não foram analisadas. Envie o arquivo para analisar o conteúdo."
            ],
        )
    except (ValueError, TypeError, AttributeError) as error:
        raise SourceError("empty") from error


async def fetch(raw):
    url = checked_url(raw)
    original = url
    endpoint = social_endpoint(url)
    if endpoint:
        url = checked_url(endpoint)
    for _ in range(5):
        address = await destination(url)
        status, headers, body = await request(url, address)
        if status == 200:
            if endpoint:
                return video_metadata(body, original)
            return extract(body, headers, url)
        location = headers.get("location")
        if not location:
            raise SourceError("redirect")
        next_url = checked_url(urljoin(str(url), location))
        if url.scheme == "https" and next_url.scheme != "https":
            raise SourceError("redirect")
        url = next_url
    raise SourceError("redirect")


async def fetch_with_retry(raw):
    for attempt in range(2):
        try:
            return await fetch(raw)
        except SourceError as error:
            if str(error) != "transient" or attempt:
                raise
        except (
            OSError,
            dns.exception.DNSException,
            httpcore.NetworkError,
            httpcore.TimeoutException,
            httpcore.ProtocolError,
        ):
            if attempt:
                raise


async def read(raw):
    try:
        return await asyncio.wait_for(fetch_with_retry(raw), timeout=TOTAL_TIMEOUT)
    except SourceError as error:
        reason = str(error)
    except (TimeoutError, dns.exception.Timeout):
        reason = "timeout"
    except (
        OSError,
        dns.exception.DNSException,
        httpcore.NetworkError,
        httpcore.TimeoutException,
        httpcore.ProtocolError,
        ValueError,
    ):
        reason = "network"
    return SourceContent(
        state="unavailable",
        url=clip(raw, 4096),
        fetched_at=datetime.now(UTC),
        limitations=[MESSAGES[reason]],
    )


def read_source(url):
    """Called only in the worker's synchronous task thread, never while capturing."""
    return asyncio.run(read(url))


def evidence(text, query, limit=700):
    """A bounded source excerpt for verification, preferring paragraphs matching the query."""
    terms = set(re.findall(r"\w{4,}", query.casefold()))
    chunks = [text[start : start + 400] for start in range(0, len(text), 400)]
    best = sorted(
        enumerate(chunks),
        key=lambda part: (-sum(term in part[1].casefold() for term in terms), part[0]),
    )
    # Keep introductory context and one matched passage (in original source order).
    indices = sorted({0, best[0][0]}) if best else []
    return clip("\n[…]\n".join(chunks[index] for index in indices), limit)
