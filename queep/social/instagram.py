"""Instagram posts, reels and profiles. Public JSON/HTML only; no login or Graph API."""

import json
import re
from datetime import UTC, datetime

from queep.models import SourceContent
from queep.social.types import SocialMatch

HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
POST_ROOTS = {"p", "reel", "reels", "tv"}
RESERVED = POST_ROOTS | {
    "accounts",
    "direct",
    "explore",
    "stories",
    "about",
    "legal",
    "developer",
    "developers",
    "privacy",
    "help",
    "emails",
    "session",
    "challenge",
    "lite",
    "api",
    "graphql",
    "share",
    "reels",
}
CODE = re.compile(r"[A-Za-z0-9_-]+")


def instagram_host(url):
    host = (url.host or "").lower()
    return host in HOSTS or host.endswith(".instagram.com")


def _code(value):
    if not isinstance(value, str) or not CODE.fullmatch(value):
        return None
    return value


def post_code(url):
    if not instagram_host(url):
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() in POST_ROOTS:
        return _code(parts[1])
    if len(parts) >= 3 and parts[1].lower() in POST_ROOTS:
        return _code(parts[2])
    if len(parts) >= 3 and parts[0].lower() == "share" and parts[1].lower() in POST_ROOTS:
        return _code(parts[2])
    return None


def profile_handle(url):
    if not instagram_host(url) or post_code(url):
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) != 1:
        return None
    handle = parts[0]
    if handle.lower() in RESERVED:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", handle):
        return None
    return handle


class InstagramAdapter:
    name = "instagram"

    def match(self, url):
        code = post_code(url)
        if code:
            return SocialMatch("instagram", "post", code)
        handle = profile_handle(url)
        if handle:
            return SocialMatch("instagram", "profile", handle)
        return None

    def readers(self, url, match):
        if match.role == "profile":
            return [f"https://www.instagram.com/{match.key}/"]
        code = match.key
        return [
            f"https://www.instagram.com/p/{code}/embed/captioned/",
            f"https://www.instagram.com/reel/{code}/embed/captioned/",
            str(url),
        ]

    def media(self, body, url, match):
        payload = _payload(body, url)
        if match.role == "profile":
            return [{"url": payload["avatar"], "kind": "avatar"}] if payload.get("avatar") else []
        items = []
        if payload.get("avatar"):
            items.append({"url": payload["avatar"], "kind": "avatar"})
        used_clip = False
        for slide in payload.get("slides") or []:
            item = dict(slide)
            if item.get("kind") == "video" and item.get("clip_url"):
                if used_clip:
                    item.pop("clip_url", None)
                used_clip = True
            items.append(item)
        return items

    def parse(self, body, url, match):
        from queep.sources import SourceError, clip

        payload = _payload(body, url)
        if match.role == "profile":
            name = payload.get("author") or match.key
            handle = payload.get("handle") or match.key
            return SourceContent(
                content_kind="profile",
                state="read",
                url=str(url),
                title=name[:200],
                text=clip(payload.get("caption") or "", 3000),
                author_name=name[:200],
                author_handle=handle.lstrip("@")[:32],
                native_title=True,
                fetched_at=datetime.now(UTC),
                limitations=[
                    "Foi lida a bio pública do perfil. Posts, destaques e estatísticas "
                    "não foram analisados."
                ],
            )
        author = payload.get("author") or ""
        handle = (payload.get("handle") or "").lstrip("@")
        text = (payload.get("caption") or "").strip()
        slides = payload.get("slides") or []
        if len(text) < 8 and not author and not handle and not slides:
            raise SourceError("empty")
        return SourceContent(
            content_kind="metadata",
            state="read",
            url=str(url),
            title=(author or handle or "Post")[:200],
            text=clip(text, 3000),
            author_name=author[:200],
            author_handle=handle[:32],
            native_title=False,
            fetched_at=datetime.now(UTC),
            limitations=[_post_limitation(slides, text)],
        )

    def fallback(self, url, match, previews):
        if match.role != "post" or not previews:
            return None
        handle = match.key if profile_handle(url) else ""
        return SourceContent(
            content_kind="metadata",
            state="read",
            url=str(url),
            title=(handle or "Post")[:200],
            text="",
            author_handle=handle[:32],
            native_title=False,
            fetched_at=datetime.now(UTC),
            limitations=[
                "Foi lida a mídia pública do post; o texto completo pode estar indisponível.",
                "Comentários não foram analisados.",
            ],
        )


def _post_limitation(slides, text):
    has_video = any(item.get("kind") == "video" for item in slides)
    if slides and has_video:
        return (
            "Foi lido o texto público e até 4 mídias. O áudio do vídeo só é analisado "
            "num recorte curto. Comentários não foram analisados."
        )
    if slides:
        return "Foi lido o texto público e até 4 imagens. Comentários não foram analisados."
    if text:
        return (
            "Foi lida somente a descrição pública do post. Áudio, vídeo, imagens "
            "e comentários não foram analisados. "
            "Para analisar a mídia, envie o arquivo."
        )
    return "Foi lido o autor público do post; o texto completo pode estar indisponível."


def _payload(body, page):
    empty = {"author": "", "handle": "", "caption": "", "avatar": None, "slides": []}
    if not body:
        return empty
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            parsed = _from_json(value, page)
            if parsed:
                return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    from bs4 import BeautifulSoup

    from queep.sources import meta_content

    try:
        soup = BeautifulSoup(body, "html.parser")
    except (TypeError, ValueError):
        return empty
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            parsed = _from_jsonld(json.loads(script.string or ""), page)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if parsed:
            return parsed
    for script in soup.find_all("script"):
        raw = script.string or ""
        if "shortcode_media" not in raw and "xdt_shortcode_media" not in raw:
            continue
        parsed = _from_embedded_json(raw, page)
        if parsed:
            return parsed
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    description = meta_content(soup, "og:description")
    og_title = meta_content(soup, "og:title") or title
    author, handle = _post_identity(og_title, description)
    image = _media_url(meta_content(soup, "og:image", "twitter:image"), page)
    video = _media_url(meta_content(soup, "og:video", "og:video:secure_url", "og:video:url"), page)
    poster = image
    slides = []
    if video and _looks_like_video(video):
        if poster:
            slides.append(_video_slide(poster, video))
    elif image:
        slides.append({"url": image, "kind": "photo"})
    return {
        "author": author,
        "handle": handle,
        "caption": description,
        "avatar": None,
        "slides": slides,
    }


def _from_json(value, page):
    node = _media_node(value)
    if node:
        return _from_graphql(node, page)
    if isinstance(value.get("medias"), list) or "username" in value:
        return _from_instafix(value, page)
    return None


def _media_node(value):
    if not isinstance(value, dict):
        return None
    for key in ("xdt_shortcode_media", "shortcode_media"):
        node = value.get(key)
        if isinstance(node, dict):
            return node
    data = value.get("data")
    if isinstance(data, dict):
        return _media_node(data)
    graphql = value.get("graphql")
    if isinstance(graphql, dict):
        return _media_node(graphql)
    return None


def _from_graphql(node, page):
    owner = node.get("owner") if isinstance(node.get("owner"), dict) else {}
    handle = owner.get("username") if isinstance(owner.get("username"), str) else ""
    author = owner.get("full_name") if isinstance(owner.get("full_name"), str) else ""
    avatar = _media_url(owner.get("profile_pic_url"), page)
    caption = _caption(node)
    slides, seen = [], set()
    children = None
    sidecar = node.get("edge_sidecar_to_children")
    if isinstance(sidecar, dict) and isinstance(sidecar.get("edges"), list):
        children = [edge.get("node") for edge in sidecar["edges"] if isinstance(edge, dict)]
    carousel = node.get("carousel_media")
    if not children and isinstance(carousel, list):
        children = carousel
    sequence = [item for item in (children or [node]) if isinstance(item, dict)]
    for item in sequence:
        slide = _slide(item, page, seen)
        if slide:
            slides.append(slide)
        if len(slides) >= 4:
            break
    return {
        "author": (author or handle).strip(),
        "handle": handle.lstrip("@"),
        "caption": caption,
        "avatar": avatar,
        "slides": slides,
    }


def _from_instafix(value, page):
    handle = value.get("username") if isinstance(value.get("username"), str) else ""
    caption = value.get("caption") if isinstance(value.get("caption"), str) else ""
    avatar = _media_url(value.get("profile_pic_url") or value.get("avatar"), page)
    slides, seen = [], set()
    medias = value.get("medias")
    if not isinstance(medias, list):
        medias = []
    for item in medias:
        if not isinstance(item, dict):
            continue
        slide = _slide(item, page, seen)
        if slide:
            slides.append(slide)
        if len(slides) >= 4:
            break
    return {
        "author": handle,
        "handle": handle.lstrip("@"),
        "caption": caption,
        "avatar": avatar,
        "slides": slides,
    }


def _from_jsonld(value, page):
    if isinstance(value, list):
        for item in value:
            parsed = _from_jsonld(item, page)
            if parsed and (parsed["caption"] or parsed["slides"]):
                return parsed
        return None
    if not isinstance(value, dict):
        return None
    author, handle = "", ""
    raw_author = value.get("author")
    if isinstance(raw_author, dict):
        if isinstance(raw_author.get("name"), str):
            author = raw_author["name"].strip()
        ident = raw_author.get("identifier")
        if isinstance(ident, dict) and isinstance(ident.get("value"), str):
            handle = ident["value"].lstrip("@")
        elif isinstance(raw_author.get("alternateName"), str):
            handle = raw_author["alternateName"].lstrip("@")
    caption = value.get("caption") or value.get("articleBody") or value.get("description") or ""
    if not isinstance(caption, str):
        caption = ""
    slides, seen = [], []
    images = value.get("image")
    if isinstance(images, str):
        images = [images]
    if isinstance(images, list):
        for raw in images:
            url = raw.get("url") if isinstance(raw, dict) else raw
            address = _media_url(url, page)
            if address and address not in seen:
                seen.append(address)
                slides.append({"url": address, "kind": "photo"})
            if len(slides) >= 4:
                break
    video = _media_url(value.get("contentUrl"), page)
    poster = _media_url(value.get("thumbnailUrl"), page) or (slides[0]["url"] if slides else None)
    if video and _looks_like_video(video) and poster:
        slides = [_video_slide(poster, video)] + [
            item for item in slides if item["url"] != poster
        ][:3]
    return {
        "author": author or handle,
        "handle": handle,
        "caption": caption,
        "avatar": None,
        "slides": slides[:4],
    }


def _from_embedded_json(raw, page):
    for match in re.finditer(r'\{[^{}]{0,200}"(?:shortcode_media|xdt_shortcode_media)"', raw):
        start = raw.rfind("{", 0, match.start() + 1)
        if start < 0:
            continue
        blob = _balanced_object(raw, start)
        if not blob:
            continue
        try:
            parsed = _from_json(json.loads(blob), page)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if parsed:
            return parsed
    return None


def _balanced_object(raw, start):
    depth, in_string, escape = 0, False, False
    for index, char in enumerate(raw[start : start + 400000], start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1]
    return None


def _caption(node):
    if isinstance(node.get("caption"), str):
        return node["caption"]
    if isinstance(node.get("caption"), dict) and isinstance(node["caption"].get("text"), str):
        return node["caption"]["text"]
    edges = node.get("edge_media_to_caption")
    if isinstance(edges, dict) and isinstance(edges.get("edges"), list) and edges["edges"]:
        first = edges["edges"][0]
        if isinstance(first, dict) and isinstance(first.get("node"), dict):
            text = first["node"].get("text")
            if isinstance(text, str):
                return text
    return ""


def _slide(item, page, seen):
    typename = str(item.get("type") or item.get("__typename") or item.get("typeName") or "")
    explicit_video = bool(item.get("is_video")) or "Video" in typename
    video = _media_url(item.get("video_url") or (item.get("url") if explicit_video else None), page)
    if not video:
        versions = item.get("video_versions")
        if isinstance(versions, list) and versions and isinstance(versions[0], dict):
            video = _media_url(versions[0].get("url"), page)
    poster = _media_url(
        item.get("display_url")
        or item.get("thumbnail_src")
        or item.get("thumbnail_url")
        or _candidate_url(item),
        page,
    )
    is_video = explicit_video or bool(video and _looks_like_video(video))
    if is_video:
        clip = video if video and _looks_like_video(video) else None
        if poster and _looks_like_video(poster):
            poster = None
        if poster:
            if poster in seen:
                return None
            seen.add(poster)
            return _video_slide(poster, clip)
        if clip:
            return {"url": clip, "kind": "video", "clip_url": clip}
        return None
    photo = poster or _media_url(item.get("url"), page)
    if not photo or photo in seen or _looks_like_video(photo):
        return None
    seen.add(photo)
    return {"url": photo, "kind": "photo"}


def _candidate_url(item):
    versions = item.get("image_versions2")
    if isinstance(versions, dict) and isinstance(versions.get("candidates"), list):
        first = versions["candidates"][0] if versions["candidates"] else None
        if isinstance(first, dict):
            return first.get("url")
    return None


def _video_slide(poster, clip):
    item = {"url": poster, "kind": "video"}
    if clip:
        item["clip_url"] = clip
    return item


def _media_url(raw, page):
    from queep.sources import public_asset_url

    if isinstance(raw, dict):
        raw = raw.get("url")
    return public_asset_url(raw, page)


def _looks_like_video(url):
    path = url.split("?", 1)[0].lower()
    name = path.rsplit("/", 1)[-1]
    return path.endswith(".mp4") or "/video" in path or "video" in name


def _post_identity(og_title, description):
    author, handle = "", ""
    value = og_title or ""
    for marker in (" on Instagram", " no Instagram", " • Instagram"):
        if marker in value:
            value = value.split(marker, 1)[0]
            break
    value = value.strip()
    if value.startswith("@") and " " not in value:
        handle = value.lstrip("@")
    elif value:
        author = value
    if not handle:
        found = re.search(r"@([A-Za-z0-9._]{1,30})", description or "")
        if found:
            handle = found.group(1)
    return author, handle
