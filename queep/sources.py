"""Bounded public text extraction. No browser, credentials, cookies or subresource fetches."""

import asyncio
import ipaddress
import json
import logging
import re
import socket
import ssl
import zlib
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlencode, urljoin

import dns.asyncresolver
import dns.exception
import httpcore
import httpx
from bs4 import BeautifulSoup

from queep.models import SourceContent

logger = logging.getLogger(__name__)

MAX_BODY = 2 * 1024 * 1024
MAX_TEXT = 12000  # UTF-8 bytes, before any model-specific clipping.
TOTAL_TIMEOUT = 20
READ_LIMITATION = (
    "Foi lido o texto acessível, inclusive o hero e o CTA. A imagem de preview entra na "
    "análise quando está disponível. Áudio e vídeo da página não foram baixados."
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


TEXT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/json",
)
IMAGE_TYPES = (
    "image/jpeg",
    "image/jpg",
    "image/pjpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/x-icon",
    "image/vnd.microsoft.icon",
)
VIDEO_TYPES = ("video/mp4", "application/octet-stream")
PHOTO_HOSTS = {"pbs.twimg.com"}
VIDEO_HOSTS = {"video.twimg.com"}
MAX_PHOTO = 12 * 1024 * 1024
MAX_SOCIAL_CLIP = 25 * 1024 * 1024


def _tls_context():
    """Stdlib HTTPS handshake. A ClientHello without HTTP/1.1 ALPN is challenged."""
    context = ssl.create_default_context()
    context.set_alpn_protocols(["http/1.1"])
    if context.post_handshake_auth is not None:
        context.post_handshake_auth = True
    return context


async def request(url, address, allowed=TEXT_TYPES, max_body=MAX_BODY):
    pinned = url.copy_with(host=address)
    accept = (
        "image/jpeg,image/png,image/webp,image/gif,image/x-icon,image/vnd.microsoft.icon"
        if allowed == IMAGE_TYPES
        else "video/mp4,application/octet-stream"
        if allowed == VIDEO_TYPES
        else "text/html,application/xhtml+xml,text/plain,text/markdown,application/json"
    )
    headers = {
        "Host": url.netloc.decode(),
        "User-Agent": "Queep/0.1 (reference reader)",
        "Accept": accept,
        "Accept-Encoding": "gzip, identity",
    }
    # HTTP core has no environment proxy/cookie jar. A fresh pool per hop prevents
    # sharing a connection across virtual hosts. TLS still verifies the original host.
    async with httpcore.AsyncConnectionPool(retries=0, ssl_context=_tls_context()) as pool:
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
            if kind not in allowed:
                raise SourceError("format")
            encoding = metadata.get("content-encoding", "identity").lower()
            if encoding not in ("identity", "gzip"):
                raise SourceError("format")
            length = metadata.get("content-length", "")
            # Pages keep a bounded prefix: script shells often gzip past the cap
            # while the title and Open Graph tags sit in the first bytes.
            # Images and video stay a hard limit — a partial file is not usable.
            bounded_text = kind in TEXT_TYPES
            if not bounded_text and length.isdecimal() and int(length) > max_body:
                raise SourceError("size")
            body = bytearray()
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
            wire_bytes = 0
            stopped = False
            async for chunk in response.aiter_stream():
                cut_wire = False
                if bounded_text:
                    room = max_body - wire_bytes
                    if room <= 0:
                        stopped = True
                        break
                    if len(chunk) > room:
                        chunk = chunk[:room]
                        cut_wire = True
                wire_bytes += len(chunk)
                if not bounded_text and wire_bytes > max_body:
                    raise SourceError("size")
                if decoder:
                    if bounded_text and len(body) >= max_body:
                        stopped = True
                        break
                    limit = (
                        max_body - len(body) if bounded_text else max_body + 1 - len(body)
                    )
                    try:
                        chunk = decoder.decompress(chunk, limit)
                    except zlib.error as error:
                        raise SourceError("format") from error
                    if bounded_text and (
                        decoder.unconsumed_tail or len(body) + len(chunk) >= max_body
                    ):
                        stopped = True
                if not bounded_text and len(body) + len(chunk) > max_body:
                    raise SourceError("size")
                if bounded_text and len(body) + len(chunk) > max_body:
                    chunk = chunk[: max_body - len(body)]
                    stopped = True
                body.extend(chunk)
                if stopped or cut_wire:
                    stopped = True
                    break
            if decoder and not stopped and (not decoder.eof or decoder.unused_data):
                raise SourceError("format")
            return response.status, metadata, bytes(body)


_CHROME_LABEL = re.compile(
    r"^(?:log[\s-]?in|sign[\s-]?in|sign[\s-]?up|menu|cookies?|accept(?: all)?|"
    r"entrar|cadastre-se|cadastrar|aceitar(?: tudo)?|newsletter|subscribe|"
    r"close|fechar|skip|pular)$",
    re.I,
)


def _line(value):
    return re.sub(r"\s+", " ", value or "").strip()


def hero_lines(soup):
    """Headline and CTA text. Nav chrome such as Log in / Sign up is left out."""
    lines = []

    def add(value):
        text = _line(value)
        if not text or len(text) > 180 or _CHROME_LABEL.match(text):
            return
        if text not in lines:
            lines.append(text)

    for node in soup.find_all("h1"):
        add(node.get_text(" ", strip=True))
    header = soup.find("header")
    if header:
        for node in header.find_all(["h2", "p"]):
            add(node.get_text(" ", strip=True))
    for node in soup.select(
        "button, a[class*='btn'], a[class*='button'], a[class*='cta'], [role='button']"
    ):
        add(node.get_text(" ", strip=True))
    return lines[:8]


def extract(body, metadata, url):
    kind = metadata.get("content-type", "").split(";", 1)[0].lower()
    title = ""
    if kind == "application/json":
        raise SourceError("format")
    if kind in ("text/plain", "text/markdown"):
        text = body.decode("utf-8", errors="replace").strip()
        if len(text) < 80:
            raise SourceError("empty")
    else:
        soup = BeautifulSoup(body, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        if re.search(
            r"just a moment|access denied|\bsign in\b|\blog in\b|attention required|"
            r"verify you are human|iniciar sess[aã]o",
            title,
            re.I,
        ):
            raise SourceError("empty")
        product = page_product_name(soup, title)
        description = meta_content(
            soup, "og:description", "twitter:description", "description"
        )
        hero = hero_lines(soup)
        for node in soup.select(
            "script,style,noscript,template,nav,footer,aside,"
            "form,svg,[hidden],[aria-hidden='true']"
        ):
            node.decompose()
        for node in soup.select("button, [role='button']"):
            if _CHROME_LABEL.match(_line(node.get_text(" ", strip=True))):
                node.decompose()
        # GitHub renders public README content here; no code execution or repository crawl.
        content = soup.select_one("article.markdown-body") or soup.article or soup.main or soup.body
        if not content:
            raise SourceError("empty")
        text = "\n".join(
            line.strip() for line in content.get_text("\n", strip=True).splitlines() if line.strip()
        )
        kept = [line for line in hero if line not in text]
        if kept:
            text = "Hero: " + " | ".join(kept) + "\n" + text
        title = product or title
        if len(text) < 80:
            summary = _meta_summary(description)
            if len(summary) < 24 or _blocked_summary(summary):
                raise SourceError("empty")
            text = summary
    truncated = len(text.encode()) > MAX_TEXT
    limitations = [READ_LIMITATION]
    if truncated:
        limitations.append("A página é longa; somente um trecho foi armazenado e analisado.")
    return SourceContent(
        content_kind="page",
        state="read",
        url=str(url),
        title=title[:200],
        text=clip(text),
        fetched_at=datetime.now(UTC),
        truncated=truncated,
        native_title=False,
        limitations=limitations,
    )


def _meta_summary(value):
    if not isinstance(value, str) or not value.strip():
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _blocked_summary(value):
    return bool(re.search(
        r"just a moment|access denied|attention required|verify you are human|"
        r"iniciar sess[aã]o",
        value,
        re.I,
    ))


def meta_content(soup, *names):
    for name in names:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        value = tag.get("content") if tag else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def page_product_name(soup, page_title):
    name = meta_content(soup, "og:site_name", "application-name")
    if name:
        return name[:200]
    if not page_title:
        return ""
    return re.split(r"\s*[|\u2013\u2014\-]\s*", page_title, maxsplit=1)[0].strip()[:200]


def public_asset_url(raw, page):
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or value.lower().startswith(("data:", "javascript:")):
        return None
    try:
        url = checked_url(urljoin(str(page), value))
    except SourceError:
        return None
    try:
        if not public_ip(url.host):
            return None
    except ValueError:
        pass
    return str(url)


def page_previews(body, page):
    """Favicon and og:image for website cards; never page chrome or private hosts."""
    try:
        soup = BeautifulSoup(body, "html.parser")
    except (TypeError, ValueError):
        return []
    previews, seen = [], set()

    def add(raw, kind):
        address = public_asset_url(raw, page)
        if not address or address in seen or kind not in ("photo", "avatar"):
            return
        seen.add(address)
        previews.append({"url": address, "kind": kind})

    add(meta_content(soup, "og:image", "twitter:image", "twitter:image:src"), "photo")
    icons = []
    for tag in soup.find_all("link"):
        rels = tag.get("rel") or []
        if isinstance(rels, str):
            rels = [rels]
        names = {str(part).lower() for part in rels}
        href = tag.get("href")
        mime = (tag.get("type") or "").lower()
        if "svg" in mime:
            continue
        if "apple-touch-icon" in names:
            icons.append((0, href))
        elif "icon" in names and "png" in mime:
            icons.append((1, href))
        elif "icon" in names or "shortcut icon" in names:
            icons.append((2, href))
    icons.sort(key=lambda item: item[0])
    if icons:
        add(icons[0][1], "avatar")
    else:
        add("/favicon.ico", "avatar")
    return previews


TWEET_HOSTS = {
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
}


RESERVED_PROFILE_PATHS = {
    "about",
    "account",
    "api",
    "articles",
    "communities",
    "compose",
    "download",
    "explore",
    "followers",
    "following",
    "hashtag",
    "hashtags",
    "help",
    "home",
    "i",
    "intent",
    "jobs",
    "likes",
    "lists",
    "login",
    "messages",
    "moments",
    "notifications",
    "oauth",
    "privacy",
    "search",
    "settings",
    "share",
    "signup",
    "status",
    "topics",
    "tos",
    "web",
}


def tweet_status_id(url):
    """Numeric status ID from an X/Twitter post URL; never from query strings or other hosts."""
    if url.host not in TWEET_HOSTS:
        return None
    match = re.search(r"/status/([0-9]{5,20})(?:/|$)", url.path)
    return match.group(1) if match else None


def twitter_profile_handle(url):
    """Public @handle from x.com/name; never status, i/, search, hashtag or intent paths."""
    if url.host not in TWEET_HOSTS:
        return None
    if tweet_status_id(url):
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) != 1:
        return None
    handle = parts[0]
    if handle.lower() in RESERVED_PROFILE_PATHS:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return None
    return handle


def social_endpoint(url):
    """Only canonical public IDs; never pass arbitrary addresses to an oEmbed proxy."""
    tweet_id = tweet_status_id(url)
    if tweet_id:
        return "https://api.fxtwitter.com/status/" + tweet_id
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


def tweet_readers(url):
    tweet_id = tweet_status_id(url)
    if not tweet_id:
        return []
    oembed = "https://publish.x.com/oembed?" + urlencode(
        {"url": "https://twitter.com/i/web/status/" + tweet_id, "omit_script": "true"}
    )
    return [
        "https://api.fxtwitter.com/status/" + tweet_id,
        oembed,
    ]


def profile_readers(url):
    handle = twitter_profile_handle(url)
    if not handle:
        return []
    return ["https://api.fxtwitter.com/2/profile/" + handle]


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
                "Somente título, autor e a capa pública foram lidos. A fala e as cenas do vídeo "
                "não foram analisadas. Envie o arquivo para analisar o conteúdo."
            ],
            native_title=True,
        )
    except (ValueError, TypeError, AttributeError) as error:
        raise SourceError("empty") from error


def video_posters(body, page):
    """Public oEmbed poster and site icon. Private or relative junk is dropped."""
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        return []
    if not isinstance(value, dict):
        return []
    previews = []
    thumb = public_asset_url(value.get("thumbnail_url"), page)
    if thumb and not thumb.split("?", 1)[0].lower().endswith(".svg"):
        previews.append({"url": thumb, "kind": "video"})
    host = (page.host or "").lower()
    if host in ("youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com"):
        icon = "https://www.youtube.com/favicon.ico"
    elif host in ("vimeo.com", "www.vimeo.com"):
        icon = "https://vimeo.com/favicon.ico"
    else:
        icon = None
    icon_url = public_asset_url(icon, page) if icon else None
    if icon_url:
        previews.append({"url": icon_url, "kind": "avatar"})
    return previews


def clean_tweet_text(text):
    """Keep the author's line breaks; drop t.co / pic media suffixes and page chrome."""
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"https://t\.co/\w+", stripped):
            continue
        if re.fullmatch(r"pic\.(?:twitter|x)\.com/\w+", stripped):
            continue
        if re.fullmatch(r"https://(?:www\.)?(?:x|twitter)\.com/\S+/status/\d+/photo/\d+", stripped):
            continue
        kept.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def tweet_photo_items(media):
    """Carousel photos from fxtwitter `photos`, falling back to `all` photo entries."""
    items = []
    if not isinstance(media, dict):
        return items
    photos = media.get("photos")
    if isinstance(photos, dict):
        photos = [photos]
    if isinstance(photos, list):
        items.extend(photos)
    all_media = media.get("all")
    if isinstance(all_media, list):
        items.extend(
            item for item in all_media
            if isinstance(item, dict) and item.get("type") == "photo"
        )
    return items


def tweet_mosaic_media_ids(media):
    """Photo ids encoded in fxtwitter mosaic URLs when `photos` is truncated."""
    mosaic = media.get("mosaic") if isinstance(media, dict) else None
    formats = mosaic.get("formats") if isinstance(mosaic, dict) else None
    if not isinstance(formats, dict):
        return []
    raw = formats.get("jpeg") or next(
        (value for value in formats.values() if isinstance(value, str)), None
    )
    if not isinstance(raw, str):
        return []
    try:
        url = httpx.URL(raw)
    except Exception:
        return []
    if url.host != "mosaic.fxtwitter.com":
        return []
    parts = [part for part in url.path.split("/") if part]
    if len(parts) < 3:
        return []
    ids = []
    for part in parts[2:]:
        if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", part) and not part.isdigit():
            ids.append(part)
    return ids[:4]


def tweet_video_items(media):
    """fxtwitter `videos` entries; GIFs are published as videos."""
    items = []
    if not isinstance(media, dict):
        return items
    videos = media.get("videos")
    if isinstance(videos, dict):
        videos = [videos]
    if isinstance(videos, list):
        items.extend(videos)
    return items


def tweet_video_thumbnail(item):
    """Poster on the photo CDN only; never the mp4 on video.twimg.com."""
    if not isinstance(item, dict):
        return None
    for key in ("thumbnail_url", "thumbnail"):
        raw = item.get(key)
        if isinstance(raw, str):
            return raw
    return None


def tweet_clip_url(item):
    """First public mp4 host only; used for a 15s speech clip, never stored for playback."""
    if not isinstance(item, dict):
        return None
    raw = item.get("url")
    if not isinstance(raw, str):
        return None
    try:
        url = checked_url(raw)
    except SourceError:
        return None
    if url.host not in VIDEO_HOSTS:
        return None
    return str(url)


def tweet_avatar_url(body):
    try:
        value = json.loads(body)
        tweet = value.get("tweet") if isinstance(value.get("tweet"), dict) else {}
        author = tweet.get("author") if isinstance(tweet, dict) else None
        if not isinstance(author, dict):
            return None
        raw = author.get("avatar_url") or author.get("avatar")
        if not isinstance(raw, str):
            return None
        url = checked_url(raw)
        if url.host not in PHOTO_HOSTS:
            return None
        return str(url)
    except (TypeError, ValueError, json.JSONDecodeError, SourceError):
        return None


def tweet_media_previews(body):
    """Public photo and video-poster URLs in carousel order; at most four, never private hosts."""
    try:
        value = json.loads(body)
        tweet = value.get("tweet") if isinstance(value.get("tweet"), dict) else {}
        media = tweet.get("media") if isinstance(tweet, dict) else None
        previews, seen = [], set()

        def add(raw, kind, clip=None):
            if len(previews) >= 4 or not isinstance(raw, str):
                return
            if kind not in ("photo", "video", "gif"):
                return
            try:
                url = checked_url(raw)
            except SourceError:
                return
            if url.host not in PHOTO_HOSTS:
                return
            address = str(url)
            key = address.rsplit("/", 1)[-1].split(".", 1)[0].split("?", 1)[0]
            if address in seen or key in seen:
                return
            seen.add(address)
            seen.add(key)
            item = {"url": address, "kind": kind}
            if kind in ("video", "gif") and clip:
                item["clip_url"] = clip
            previews.append(item)

        sequence = []
        all_media = media.get("all") if isinstance(media, dict) else None
        if isinstance(all_media, list) and all_media:
            sequence = all_media
        else:
            sequence.extend(tweet_photo_items(media))
            sequence.extend(tweet_video_items(media))
        for item in sequence:
            if not isinstance(item, dict):
                continue
            media_type = item.get("type")
            if media_type == "gif":
                clip = tweet_clip_url(item)
                if clip:
                    add(tweet_video_thumbnail(item), "gif", clip=clip)
                else:
                    add(tweet_video_thumbnail(item), "photo")
            elif media_type == "video":
                add(tweet_video_thumbnail(item), "video", clip=tweet_clip_url(item))
            elif media_type == "photo":
                add(item.get("url"), "photo")
            elif tweet_video_thumbnail(item):
                add(tweet_video_thumbnail(item), "video", clip=tweet_clip_url(item))
            else:
                add(item.get("url"), "photo")
        if isinstance(media, dict):
            for media_id in tweet_mosaic_media_ids(media):
                add(f"https://pbs.twimg.com/media/{media_id}.jpg?name=orig", "photo")
        return previews
    except (TypeError, ValueError, json.JSONDecodeError, SourceError):
        return []


def tweet_photo_urls(body):
    """Public CDN photo URLs in carousel order; at most four, never private hosts."""
    return [item["url"] for item in tweet_media_previews(body) if item["kind"] == "photo"]


def tweet_photo_url(body):
    urls = tweet_photo_urls(body)
    return urls[0] if urls else None


def tweet_handle_from_url(url):
    """Public @handle from /user/status/id; ignore i/web and non-status paths."""
    try:
        parsed = url if hasattr(url, "host") else checked_url(str(url))
    except SourceError:
        return None
    if parsed.host not in TWEET_HOSTS:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[1] != "status":
        return None
    handle = parts[0]
    if handle in ("i", "web") or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
        return None
    return handle


def tweet_authors(value, original):
    """Display name and handle from fxtwitter/oEmbed, then the status URL."""
    name, handle = "", ""
    tweet = value.get("tweet") if isinstance(value, dict) else None
    if isinstance(tweet, dict) and isinstance(tweet.get("author"), dict):
        author = tweet["author"]
        if isinstance(author.get("name"), str):
            name = author["name"].strip()
        screen = author.get("screen_name") or author.get("username")
        if isinstance(screen, str):
            handle = screen.strip().lstrip("@")
    if isinstance(value, dict):
        if not name and isinstance(value.get("author_name"), str):
            name = value["author_name"].strip()
        if not handle and isinstance(value.get("author_url"), str):
            handle = tweet_handle_from_url(value["author_url"]) or handle
    if not handle:
        handle = tweet_handle_from_url(original) or ""
    return name, handle


def tweet_photo_fallback(original, photos):
    """Keep public photos when tweet text cannot be parsed; still surface the author handle."""
    handle = tweet_handle_from_url(original) or ""
    return SourceContent(
        content_kind="metadata",
        state="read",
        url=str(original),
        title=(handle or "Post")[:200],
        text="",
        author_handle=handle,
        native_title=False,
        fetched_at=datetime.now(UTC),
        limitations=[
            "Foi lido o conteúdo público do post; o texto completo pode estar indisponível.",
            "Respostas não foram analisadas.",
        ],
    )


def tweet_metadata(body, original):
    """Public post body and author only; never store embed HTML, media URLs or replies."""
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("metadata")
        text = ""
        tweet = value.get("tweet")
        if isinstance(tweet, dict):
            raw = tweet.get("text")
            if isinstance(raw, dict):
                raw = raw.get("text")
            if isinstance(raw, str):
                text = clean_tweet_text(raw)
        if not text and isinstance(value.get("html"), str):
            soup = BeautifulSoup(value["html"], "html.parser")
            paragraph = soup.find("p")
            extracted = paragraph.get_text("\n", strip=True) if paragraph else ""
            text = clean_tweet_text(extracted)
        name, handle = tweet_authors(value, original)
        from_payload = (
            isinstance(tweet, dict)
            or isinstance(value.get("author_name"), str)
            or isinstance(value.get("html"), str)
        )
        if not from_payload:
            raise ValueError("metadata")
        if not name:
            name = handle
        if not name and len(text) < 8:
            raise ValueError("metadata")
        return SourceContent(
            content_kind="metadata",
            state="read",
            url=str(original),
            title=name[:200],
            text=clip(text, 3000),
            author_name=name[:200],
            author_handle=handle,
            native_title=False,
            fetched_at=datetime.now(UTC),
            limitations=[
                "Foi lido o texto público do post. Respostas não foram analisadas."
                if text else
                "Foi lido o autor público do post; o texto completo pode estar indisponível."
            ],
        )
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise SourceError("empty") from error


def profile_avatar_url(body):
    """Public CDN avatar only; never banner or private hosts."""
    try:
        value = json.loads(body)
        user = value.get("user") if isinstance(value.get("user"), dict) else {}
        raw = user.get("avatar_url") or user.get("avatar")
        if not isinstance(raw, str):
            return None
        url = checked_url(raw)
        if url.host not in PHOTO_HOSTS:
            return None
        address = str(url)
        if "_normal." in address:
            address = address.replace("_normal.", "_400x400.")
        return address
    except (TypeError, ValueError, json.JSONDecodeError, SourceError):
        return None


def profile_metadata(body, original):
    """Public name, handle and bio only; never banner, tweets or follower counts."""
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("metadata")
        user = value.get("user")
        if not isinstance(user, dict):
            raise ValueError("metadata")
        name, handle, bio = "", "", ""
        if isinstance(user.get("name"), str):
            name = user["name"].strip()
        screen = user.get("screen_name") or user.get("username")
        if isinstance(screen, str):
            handle = screen.strip().lstrip("@")
        if isinstance(user.get("description"), str):
            bio = user["description"].strip()
        if not handle:
            handle = twitter_profile_handle(original) or ""
        if not name:
            name = handle
        if not name:
            raise ValueError("metadata")
        return SourceContent(
            content_kind="profile",
            state="read",
            url=str(original),
            title=name[:200],
            text=clip(bio, 3000),
            author_name=name[:200],
            author_handle=handle[:32],
            native_title=True,
            fetched_at=datetime.now(UTC),
            limitations=[
                "Foi lida a bio pública do perfil. Posts, banner e estatísticas "
                "não foram analisados."
            ],
        )
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as error:
        raise SourceError("empty") from error


async def fetch_resolved_body(url):
    for _ in range(5):
        address = await destination(url)
        status, headers, body = await request(url, address)
        if status == 200:
            return body, headers, url
        location = headers.get("location")
        if not location:
            raise SourceError("redirect")
        next_url = checked_url(urljoin(str(url), location))
        if url.scheme == "https" and next_url.scheme != "https":
            raise SourceError("redirect")
        url = next_url
    raise SourceError("redirect")


async def fetch_photo(raw):
    url = checked_url(raw)
    if url.host not in PHOTO_HOSTS:
        raise SourceError("address")
    for _ in range(3):
        address = await destination(url)
        status, headers, body = await request(
            url, address, allowed=IMAGE_TYPES, max_body=MAX_PHOTO
        )
        if status == 200:
            return body
        location = headers.get("location")
        if not location:
            raise SourceError("redirect")
        next_url = checked_url(urljoin(str(url), location))
        if next_url.host not in PHOTO_HOSTS:
            raise SourceError("redirect")
        if url.scheme == "https" and next_url.scheme != "https":
            raise SourceError("redirect")
        url = next_url
    raise SourceError("redirect")


async def fetch_public_image(raw):
    """Favicon or og:image from a public host. Same SSRF checks as pages, not Twitter-only."""
    url = checked_url(raw)
    for _ in range(3):
        address = await destination(url)
        status, headers, body = await request(
            url, address, allowed=IMAGE_TYPES, max_body=MAX_PHOTO
        )
        if status == 200:
            return body
        location = headers.get("location")
        if not location:
            raise SourceError("redirect")
        next_url = checked_url(urljoin(str(url), location))
        if url.scheme == "https" and next_url.scheme != "https":
            raise SourceError("redirect")
        url = next_url
    raise SourceError("redirect")


async def fetch_social_clip(raw):
    """Bounded mp4 from an allowlisted video CDN. Not stored; worker keeps a 15s speech clip."""
    url = checked_url(raw)
    if url.host not in VIDEO_HOSTS:
        raise SourceError("address")
    return await _fetch_video_bytes(url, hosts=VIDEO_HOSTS)


async def fetch_public_clip(raw):
    """Bounded public mp4 for social networks whose CDN is not Twitter. Same SSRF as pages."""
    return await _fetch_video_bytes(checked_url(raw), hosts=None)


async def _fetch_video_bytes(url, hosts):
    for _ in range(3):
        address = await destination(url)
        status, headers, body = await request(
            url, address, allowed=VIDEO_TYPES, max_body=MAX_SOCIAL_CLIP
        )
        if status == 200:
            return body
        location = headers.get("location")
        if not location:
            raise SourceError("redirect")
        next_url = checked_url(urljoin(str(url), location))
        if hosts is not None and next_url.host not in hosts:
            raise SourceError("redirect")
        if url.scheme == "https" and next_url.scheme != "https":
            raise SourceError("redirect")
        url = next_url
    raise SourceError("redirect")


def with_author_avatar(body, previews):
    avatar = tweet_avatar_url(body)
    if avatar and not any(item.get("kind") == "avatar" for item in previews):
        return [{"url": avatar, "kind": "avatar"}, *previews]
    return previews


def social_miss(url):
    """A known network whose path is not a public post or profile. Never a website."""
    return (
        SourceContent(
            content_kind="metadata",
            state="unavailable",
            url=str(url),
            fetched_at=datetime.now(UTC),
            limitations=["Este endereço da rede não é um post ou perfil público."],
        ),
        None,
    )


async def fetch(raw):
    original = checked_url(raw)
    from queep.social import owner, resolve

    adapter, match = resolve(original)
    if adapter:
        return await fetch_network(adapter, original, match)
    if owner(original):
        return social_miss(original)
    endpoint = social_endpoint(original)
    if endpoint:
        body, _, _ = await fetch_resolved_body(checked_url(endpoint))
        return video_metadata(body, original), video_posters(body, original) or None
    return await fetch_site(original)


async def fetch_site(original):
    """Public page: body text, or the Open Graph summary when the page is a script shell."""
    body, headers, final = await fetch_resolved_body(original)
    source = extract(body, headers, final)
    if source.content_kind != "page":
        return source, None
    return source, page_previews(body, final) or None


async def fetch_network(adapter, original, match):
    last = None
    previews = []
    # Behance oEmbed often has the author and no thumbnail. Keep that text and
    # let the official embed reader supply the cover.
    saved = None
    for endpoint in adapter.readers(original, match):
        try:
            body, _, _ = await fetch_resolved_body(checked_url(endpoint))
        except SourceError as error:
            last = error
            continue
        try:
            media = adapter.media(body, original, match) or []
            parsed = adapter.parse(body, original, match)
        except SourceError as error:
            last = error
            continue
        except Exception:
            logger.warning("social_reader_failed", exc_info=True)
            last = SourceError("empty")
            continue
        visible = [item for item in media if item.get("kind") != "avatar"]
        if saved is not None:
            if visible:
                attach = getattr(adapter, "attach_gallery", None)
                if attach:
                    saved = attach(saved, media)
                return saved, await instagram_avatar(adapter, match, saved, media)
            continue
        if not visible and adapter.name == "behance" and match.role == "post":
            saved = parsed
            previews = media
            continue
        return parsed, await instagram_avatar(adapter, match, parsed, media)
    if saved is not None:
        return saved, await instagram_avatar(adapter, match, saved, previews)
    fallback = adapter.fallback(original, match, previews)
    if fallback is not None:
        return fallback, await instagram_avatar(adapter, match, fallback, previews)
    raise last or SourceError("empty")


async def instagram_avatar(adapter, match, parsed, media):
    """Public profile photo for an Instagram post whose reader had no avatar."""
    if getattr(adapter, "name", "") != "instagram" or match.role != "post":
        return media
    if any(item.get("kind") == "avatar" for item in media):
        return media
    handle = (getattr(parsed, "author_handle", "") or "").lstrip("@")
    if not handle:
        return media
    from queep.social.instagram import profile_avatar_url

    avatar = await profile_avatar_url(handle)
    if not avatar:
        return media
    return [{"url": avatar, "kind": "avatar"}, *list(media)]


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
        result = await asyncio.wait_for(fetch_with_retry(raw), timeout=TOTAL_TIMEOUT)
        if isinstance(result, tuple):
            return result
        return result, None
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
    kind = "metadata" if _social_host(raw) else "page"
    return (
        SourceContent(
            content_kind=kind,
            state="unavailable",
            url=clip(raw, 4096),
            fetched_at=datetime.now(UTC),
            limitations=[MESSAGES[reason]],
        ),
        None,
    )


def _social_host(raw):
    try:
        from queep.social import owner

        return owner(checked_url(raw)) is not None
    except SourceError:
        return False


def read_link(url):
    """Called only in the worker's synchronous task thread, never while capturing."""
    return asyncio.run(read(url))


def read_source(url):
    """Source body only; used by tests and callers that do not attach photos."""
    return read_link(url)[0]


def read_photo(url):
    """Public photo bytes from an allowlisted host. Called only on the worker thread."""
    return asyncio.run(asyncio.wait_for(fetch_photo(url), timeout=TOTAL_TIMEOUT))


def read_public_image(url):
    """Public favicon or og:image bytes. Called only on the worker thread."""
    return asyncio.run(asyncio.wait_for(fetch_public_image(url), timeout=TOTAL_TIMEOUT))


def read_social_clip(url):
    """Public social mp4 bytes from an allowlisted host. Called only on the worker thread."""
    return asyncio.run(asyncio.wait_for(fetch_social_clip(url), timeout=TOTAL_TIMEOUT))


def read_public_clip(url):
    """Public social mp4 bytes from a non-Twitter CDN. Called only on the worker thread."""
    return asyncio.run(asyncio.wait_for(fetch_public_clip(url), timeout=TOTAL_TIMEOUT))


def evidence(text, query, limit=700):
    """A bounded source excerpt for verification, preferring paragraphs matching the query."""
    terms = set(re.findall(r"\w{3,}", query.casefold()))
    chunks = [text[start : start + 400] for start in range(0, len(text), 400)]
    best = sorted(
        enumerate(chunks),
        key=lambda part: (-sum(term in part[1].casefold() for term in terms), part[0]),
    )
    # Keep introductory context and one matched passage (in original source order).
    indices = sorted({0, best[0][0]}) if best else []
    return clip("\n[…]\n".join(chunks[index] for index in indices), limit)
