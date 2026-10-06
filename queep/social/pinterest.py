"""Pinterest pins and creator profiles from public HTML/oEmbed only."""

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from queep.models import SourceContent
from queep.social.types import SocialMatch

PINTEREST_HOST = "pinterest.com"
SHORT_HOSTS = {"pin.it", "www.pin.it"}
RESERVED = {
    "about",
    "account",
    "accounts",
    "api",
    "business",
    "categories",
    "careers",
    "explore",
    "help",
    "ideas",
    "login",
    "oauth",
    "pin",
    "privacy",
    "search",
    "settings",
    "signup",
    "terms",
    "today",
}


def pinterest_host(url):
    host = (url.host or "").lower().rstrip(".")
    return host == PINTEREST_HOST or host.endswith("." + PINTEREST_HOST)


def pin_id(url):
    """Public pin id from a canonical/slug URL, or the opaque pin.it key."""
    host = (url.host or "").lower().rstrip(".")
    parts = [part for part in url.path.split("/") if part]
    if host in SHORT_HOSTS:
        if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", parts[0]):
            return parts[0]
        return None
    if not pinterest_host(url) or len(parts) != 2 or parts[0].lower() != "pin":
        return None
    segment = parts[1]
    matched = re.fullmatch(r"[0-9]{6,30}", segment)
    if matched:
        return segment
    matched = re.fullmatch(r".+--([0-9]{6,30})", segment)
    return matched.group(1) if matched else None


def profile_handle(url):
    """Creator handle from a one-segment Pinterest URL."""
    if not pinterest_host(url) or pin_id(url):
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) != 1:
        return None
    handle = parts[0]
    if handle.lower() in RESERVED:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{3,30}", handle):
        return None
    if not any(character.isalpha() for character in handle):
        return None
    return handle


class PinterestAdapter:
    name = "pinterest"

    def match(self, url):
        pin = pin_id(url)
        if pin:
            return SocialMatch("pinterest", "post", pin)
        handle = profile_handle(url)
        if handle:
            return SocialMatch("pinterest", "profile", handle)
        return None

    def readers(self, url, match):
        canonical = _canonical_url(url, match)
        if match.role == "profile":
            return [canonical]
        oembed = "https://www.pinterest.com/oembed.json?" + urlencode({"url": canonical})
        # Public HTML carries the full caption, original media, video and avatar.
        # The official oEmbed endpoint remains a conservative fallback.
        return [canonical, oembed]

    def media(self, body, url, match):
        payload = _payload(body, url, match)
        items = []
        if payload.get("avatar"):
            items.append({"url": payload["avatar"], "kind": "avatar"})
        if match.role == "profile":
            return items[:1]
        for slide in payload.get("slides") or []:
            items.append(dict(slide))
        return items[:5]

    def parse(self, body, url, match):
        from queep.sources import SourceError, clip

        payload = _payload(body, url, match)
        name = payload.get("author") or ""
        handle = (payload.get("handle") or "").lstrip("@")
        text = (payload.get("caption") or "").strip()
        slides = payload.get("slides") or []
        if match.role == "profile":
            name = name or handle or match.key
            handle = handle or match.key
            return SourceContent(
                content_kind="profile",
                state="read",
                url=str(url),
                title=name[:200],
                text=clip(text, 3000),
                author_name=name[:200],
                author_handle=handle[:30],
                native_title=True,
                fetched_at=datetime.now(UTC),
                limitations=[
                    "Foi lida a bio pública do perfil. Pins, pastas e estatísticas não "
                    "foram analisados."
                    if text
                    else "O perfil não publicou uma bio pública. Pins, pastas e "
                    "estatísticas não foram analisados."
                ],
            )
        if len(text) < 8 and not name and not handle and not slides:
            raise SourceError("empty")
        return SourceContent(
            content_kind="metadata",
            state="read",
            url=str(url),
            title=(name or handle or "Post")[:200],
            text=clip(text, 3000),
            author_name=name[:200],
            author_handle=handle[:30],
            native_title=False,
            fetched_at=datetime.now(UTC),
            limitations=[_pin_limitation(slides, text)],
        )

    def fallback(self, url, match, previews):
        visible = [item for item in previews if item.get("kind") != "avatar"]
        if match.role != "post" or not visible:
            return None
        return SourceContent(
            content_kind="metadata",
            state="read",
            url=str(url),
            title="",
            text="",
            native_title=False,
            fetched_at=datetime.now(UTC),
            limitations=[
                "Foi lida somente a mídia pública do Pin; autor, descrição, comentários "
                "e estatísticas não estavam disponíveis."
            ],
        )


def _canonical_url(url, match):
    if match.role == "profile":
        return f"https://www.pinterest.com/{match.key}/"
    host = (url.host or "").lower().rstrip(".")
    if host in SHORT_HOSTS:
        return f"https://pin.it/{match.key}"
    return f"https://www.pinterest.com/pin/{match.key}/"


def _pin_limitation(slides, text):
    has_video = any(item.get("kind") == "video" for item in slides)
    if slides and has_video:
        return (
            "Foi lido o texto público e até 4 mídias. O áudio do vídeo só é analisado "
            "num recorte curto. Comentários e estatísticas não foram analisados."
        )
    if slides:
        return (
            "Foi lido o texto público e até 4 imagens. Comentários e estatísticas não "
            "foram analisados."
        )
    if text:
        return (
            "Foi lida somente a descrição pública do Pin. Áudio, vídeo, imagens, "
            "comentários e estatísticas não foram analisados."
        )
    return "Foi lido o autor público do Pin; a descrição completa pode estar indisponível."


def _payload(body, page, match):
    empty = {"author": "", "handle": "", "caption": "", "avatar": None, "slides": []}
    if not body:
        return empty
    try:
        value = json.loads(body)
        if isinstance(value, dict) and (
            value.get("provider_name") == "Pinterest" or "thumbnail_url" in value
        ):
            return _from_oembed(value, page)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        soup = BeautifulSoup(body, "html.parser")
    except (TypeError, ValueError):
        return empty
    return _from_html(soup, page, match)


def _from_oembed(value, page):
    author = _plain_text(value.get("author_name"))
    handle = _handle_from_url(value.get("author_url"))
    poster = _public_asset(value.get("thumbnail_url"), page)
    slides = [{"url": poster, "kind": "photo"}] if poster else []
    return {
        "author": author,
        "handle": handle,
        "caption": _plain_text(value.get("title") or value.get("description")),
        "avatar": None,
        "slides": slides,
    }


def _from_html(soup, page, match):
    structured = _structured_items(soup)
    if match.role == "profile":
        return _profile_from_html(soup, structured, page, match)
    posting = next((item for item in structured if _has_type(item, "SocialMediaPosting")), {})
    video = next((item for item in structured if _has_type(item, "VideoObject")), {})
    pinner = _meta(soup, "pinterestapp:pinner")
    if not posting and not video and not pinner:
        return {"author": "", "handle": "", "caption": "", "avatar": None, "slides": []}
    identity = posting.get("author") if isinstance(posting.get("author"), dict) else {}
    if not identity and isinstance(video.get("creator"), dict):
        identity = video["creator"]
    author = _plain_text(identity.get("name") or _meta(soup, "author"))
    handle = _plain_text(identity.get("alternateName")).lstrip("@")
    handle = handle or _handle_from_url(identity.get("url")) or _handle_from_url(pinner)
    caption = _plain_text(
        posting.get("articleBody")
        or posting.get("description")
        or video.get("description")
        or posting.get("headline")
        or _meta(soup, "og:description", "twitter:description", "description")
        or _meta(soup, "og:title", "twitter:title")
    )
    avatar = _image_url(identity.get("image"), page)
    avatar = avatar or _avatar_from_html(soup, page, handle)
    slides = []
    clip = _public_mp4(video.get("contentUrl"), page)
    if video:
        poster = _image_url(video.get("thumbnailUrl"), page)
        if poster:
            item = {"url": poster, "kind": "video" if clip else "photo"}
            if clip:
                item["clip_url"] = clip
            slides.append(item)
    if not slides:
        for raw in _image_values(posting.get("image")):
            image = _public_asset(raw, page)
            if image:
                slides.append({"url": image, "kind": "photo"})
    if not slides:
        for raw in _meta_values(soup, "og:image", "twitter:image", "twitter:image:src"):
            image = _public_asset(raw, page)
            if image:
                slides.append({"url": image, "kind": "photo"})
    return {
        "author": author,
        "handle": handle,
        "caption": caption,
        "avatar": avatar,
        "slides": _dedupe_slides(slides)[:4],
    }


def _profile_from_html(soup, structured, page, match):
    profile = next((item for item in structured if _has_type(item, "ProfilePage")), {})
    person = profile.get("mainEntity") if isinstance(profile.get("mainEntity"), dict) else {}
    author = _plain_text(person.get("name"))
    handle = _plain_text(person.get("alternateName")).lstrip("@") or match.key
    caption = _plain_text(person.get("description"))
    avatar = _image_url(person.get("image"), page)
    og_title = _plain_text(_meta(soup, "og:title", "twitter:title"))
    if not author:
        author = re.sub(
            r"\s*\([^)]*\)\s*-\s*Profile\s*\|\s*Pinterest\s*$",
            "",
            og_title,
            flags=re.I,
        ).strip()
        author = author or re.sub(r"\s*[|\-]\s*Pinterest\s*$", "", og_title, flags=re.I).strip()
    if not caption:
        caption = _plain_text(_meta(soup, "og:description", "twitter:description", "description"))
        prefix = re.compile(rf"^{re.escape(author)}\s*[|\-]\s*", re.I) if author else None
        if prefix:
            caption = prefix.sub("", caption, count=1).strip()
    avatar = avatar or _avatar_from_html(soup, page, handle)
    return {
        "author": author,
        "handle": handle,
        "caption": caption,
        "avatar": avatar,
        "slides": [],
    }


def _structured_items(soup):
    items = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"})[:12]:
        try:
            value = json.loads(script.string or script.get_text() or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        roots = value if isinstance(value, list) else [value]
        for root in roots:
            if not isinstance(root, dict):
                continue
            items.append(root)
            graph = root.get("@graph")
            if isinstance(graph, list):
                items.extend(item for item in graph if isinstance(item, dict))
    return items[:32]


def _has_type(item, expected):
    kind = item.get("@type")
    return expected in kind if isinstance(kind, list) else kind == expected


def _plain_text(value):
    if not isinstance(value, str):
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.select("script,style,noscript,template,iframe"):
        node.decompose()
    lines = [line.strip() for line in soup.get_text("\n", strip=True).splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line for line in lines if line)).strip()


def _meta_values(soup, *names):
    wanted = {name.lower() for name in names}
    values = []
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or "").strip().lower()
        value = tag.get("content")
        if key in wanted and isinstance(value, str) and value.strip():
            values.append(value.strip())
    return values


def _meta(soup, *names):
    values = _meta_values(soup, *names)
    return values[0] if values else ""


def _handle_from_url(raw):
    from queep.sources import SourceError, checked_url

    if not isinstance(raw, str):
        return ""
    try:
        return profile_handle(checked_url(raw)) or ""
    except SourceError:
        return ""


def _public_asset(raw, page):
    from queep.sources import public_asset_url

    return public_asset_url(raw, page)


def _public_mp4(raw, page):
    from queep.sources import SourceError, checked_url

    address = _public_asset(raw, page)
    if not address:
        return None
    try:
        parsed = checked_url(address)
    except SourceError:
        return None
    return address if parsed.path.lower().endswith(".mp4") else None


def _image_values(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        raw = value.get("contentUrl") or value.get("url")
        return [raw] if isinstance(raw, str) else []
    if isinstance(value, list):
        values = []
        for item in value[:8]:
            values.extend(_image_values(item))
        return values
    return []


def _image_url(value, page):
    for raw in _image_values(value):
        image = _public_asset(raw, page)
        if image:
            return image
    return None


def _avatar_from_html(soup, page, handle):
    if not handle:
        return None
    for node in soup.find_all("img", limit=80):
        alt = (node.get("alt") or "").strip().lstrip("@").lower()
        raw = node.get("src") or node.get("data-src")
        if alt != handle.lower() or not isinstance(raw, str) or "_RS/" not in raw:
            continue
        avatar = _public_asset(raw, page)
        if avatar:
            return avatar
    return None


def _dedupe_slides(slides):
    result, seen = [], set()
    for item in slides:
        raw = item.get("url")
        if not raw:
            continue
        key = raw.rsplit("/", 1)[-1].split("?", 1)[0].lower()
        key = key or raw
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result
