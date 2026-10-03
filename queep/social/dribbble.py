"""Dribbble shots and designer profiles from public oEmbed/HTML only."""

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from queep.models import SourceContent
from queep.social.types import SocialMatch

HOSTS = {"dribbble.com", "www.dribbble.com"}
RESERVED = {
    "about", "account", "accounts", "advertise", "api", "careers", "collections",
    "contact", "designers", "directories", "explore", "freelancers", "goods",
    "help", "invitations", "jobs", "learn", "login", "marketplace", "messages",
    "notifications", "oauth", "places", "playoffs", "privacy", "pro", "projects",
    "resources", "search", "services", "session", "settings", "shots", "signup",
    "stories", "tags", "teams", "terms", "users",
}


def dribbble_host(url):
    host = (url.host or "").lower()
    return host in HOSTS or host.endswith(".dribbble.com")


def shot_id(url):
    """Numeric id from /shots/{id}-slug; never a gallery or profile path."""
    if not dribbble_host(url):
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) != 2 or parts[0].lower() != "shots":
        return None
    matched = re.fullmatch(r"([0-9]{1,20})(?:-[A-Za-z0-9][A-Za-z0-9-]*)?", parts[1])
    return matched.group(1) if matched else None


def profile_handle(url):
    """Designer handle from a one-segment Dribbble path."""
    if not dribbble_host(url) or shot_id(url):
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) != 1:
        return None
    handle = parts[0]
    if handle.lower() in RESERVED:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", handle):
        return None
    return handle


def _json(body):
    try:
        value = json.loads(body)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _plain_text(value):
    """Turn a public caption/bio into text without retaining markup or scripts."""
    if not isinstance(value, str):
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for node in soup.select("script,style,noscript,template"):
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


def _profile_from_url(raw, page=None):
    from queep.sources import SourceError, checked_url

    if not isinstance(raw, str):
        return ""
    try:
        parsed = checked_url(urljoin(str(page), raw) if page is not None else raw)
    except SourceError:
        return ""
    return profile_handle(parsed) or ""


def _public_asset(raw, page):
    from queep.sources import public_asset_url

    return public_asset_url(raw, page)


def _public_mp4(raw, page, explicit_mp4=False):
    from queep.sources import SourceError, checked_url

    address = _public_asset(raw, page)
    if not address:
        return None
    try:
        parsed = checked_url(address)
    except SourceError:
        return None
    return address if explicit_mp4 or parsed.path.lower().endswith(".mp4") else None


def _structured_items(soup):
    """Small, bounded JSON-LD reader for metadata already present in public HTML."""
    items = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"})[:8]:
        try:
            value = json.loads(script.string or script.get_text() or "")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        roots = value if isinstance(value, list) else [value]
        for root in roots:
            if not isinstance(root, dict):
                continue
            graph = root.get("@graph")
            candidates = [root] + (graph if isinstance(graph, list) else [])
            items.extend(item for item in candidates if isinstance(item, dict))
    return items[:24]


def _identity_from_html(soup, match, og_title, page):
    name = _meta(soup, "author", "article:author")
    handle = _meta(soup, "twitter:creator").lstrip("@").strip()
    for item in _structured_items(soup):
        author = item.get("author")
        if isinstance(author, list):
            author = next((entry for entry in author if isinstance(entry, dict)), None)
        if isinstance(author, dict):
            if not name and isinstance(author.get("name"), str):
                name = author["name"].strip()
            if not handle:
                handle = _profile_from_url(author.get("url"), page)
        if name and handle:
            break
    for link in soup.select(
        "a[rel~='author'], .shot-user a, .user-information a, .user-sticky-header a"
    )[:12]:
        candidate = _profile_from_url(link.get("href"), page)
        if not candidate:
            continue
        handle = handle or candidate
        label = link.get_text(" ", strip=True)
        if label and not name:
            name = label
        if name and handle:
            break
    data_handle = _meta(soup, "twitter:data1").lstrip("@").strip()
    if not handle and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", data_handle):
        handle = data_handle
    matched = re.search(r"\s+by\s+(.+?)(?:\s+on\s+Dribbble)?$", og_title, re.I)
    if matched and not name:
        name = matched.group(1).strip()
    if match.role == "profile":
        handle = handle or match.key
        if not name:
            name = re.sub(
                r"\s*(?:[|\-]\s*Dribbble|on Dribbble)\s*$", "", og_title, flags=re.I
            ).strip()
    return name, handle


def _caption_from_html(soup, og_title):
    for selector in (
        "[data-testid='shot-description']",
        "[data-shot-description]",
        ".shot-description",
        ".shot-desc",
        ".formatted-text",
    ):
        node = soup.select_one(selector)
        if node:
            text = re.sub(
                r"^(?:Description|Descrição)\s+", "", _plain_text(str(node))
            ).strip()
            if text:
                return text
    description = _plain_text(
        _meta(soup, "og:description", "twitter:description", "description")
    )
    if description:
        return description
    matched = re.match(r"(.+?)\s+by\s+.+?(?:\s+on\s+Dribbble)?$", og_title, re.I)
    return matched.group(1).strip() if matched else _plain_text(og_title)


def _profile_bio_from_html(soup):
    for selector in ("[data-profile-bio]", "[data-testid='profile-bio']", ".profile-bio"):
        node = soup.select_one(selector)
        if node:
            text = _plain_text(str(node))
            if text:
                return text
    return _plain_text(_meta(soup, "og:description", "twitter:description", "description"))


def _avatar_from_html(soup, page, match):
    candidates = _meta_values(
        soup, "author:image", "profile:image", "og:profile:image", "twitter:creator:image"
    )
    for item in _structured_items(soup):
        author = item.get("author")
        if isinstance(author, list):
            author = next((entry for entry in author if isinstance(entry, dict)), None)
        if isinstance(author, dict):
            image = author.get("image")
            if isinstance(image, dict):
                image = image.get("url") or image.get("contentUrl")
            candidates.append(image)
    for node in soup.select(
        "img.avatar, .avatar img, [data-avatar] img, .user-sticky-header img"
    )[:8]:
        candidates.append(node.get("src") or node.get("data-src"))
    if match.role == "profile":
        candidates.extend(_meta_values(soup, "og:image", "twitter:image", "twitter:image:src"))
    for raw in candidates:
        address = _public_asset(raw, page)
        if address:
            return address
    return None


def _media_from_json(value, page, match):
    previews, seen = [], set()

    def add(raw, kind, clip=None):
        if len(previews) >= 5 or kind not in ("photo", "video", "avatar"):
            return
        address = _public_asset(raw, page)
        if not address or address in seen:
            return
        seen.add(address)
        item = {"url": address, "kind": kind}
        if kind == "video" and clip:
            item["clip_url"] = clip
        previews.append(item)

    avatar = next(
        (
            value.get(key)
            for key in ("author_avatar_url", "author_thumbnail_url", "avatar_url")
            if isinstance(value.get(key), str)
        ),
        None,
    )
    if avatar:
        add(avatar, "avatar")
    if match.role == "profile":
        if not previews:
            add(value.get("thumbnail_url") or value.get("image"), "avatar")
        return previews[:1]

    clip = None
    for key in ("video_url", "content_url", "contentUrl"):
        if isinstance(value.get(key), str):
            clip = _public_mp4(value[key], page)
            if clip:
                break
    video = value.get("video")
    if isinstance(video, str):
        clip = clip or _public_mp4(video, page)
    elif isinstance(video, dict):
        raw = video.get("contentUrl") or video.get("content_url") or video.get("url")
        explicit = (video.get("encodingFormat") or video.get("type") or "").lower() == "video/mp4"
        clip = clip or _public_mp4(raw, page, explicit_mp4=explicit)

    poster = value.get("thumbnail_url") or value.get("thumbnailUrl")
    if clip and poster:
        add(poster, "video", clip=clip)
    images = value.get("images") or value.get("image")
    if isinstance(images, (str, dict)):
        images = [images]
    if not isinstance(images, list):
        images = []
    if isinstance(value.get("url"), str) and value.get("type") == "photo":
        images.insert(0, value["url"])
    if poster and not clip:
        images.insert(0, poster)
    for image in images:
        if isinstance(image, dict):
            image = image.get("url") or image.get("contentUrl")
        add(image, "photo")
        if len([item for item in previews if item["kind"] != "avatar"]) >= 4:
            break
    return previews[:5]


def _media_from_html(soup, page, match):
    previews, seen = [], set()
    avatar = _avatar_from_html(soup, page, match)
    if avatar:
        previews.append({"url": avatar, "kind": "avatar"})
        seen.add(avatar)
    if match.role == "profile":
        return previews[:1]

    video_type = _meta(soup, "og:video:type").lower() == "video/mp4"
    clips = _meta_values(soup, "og:video", "og:video:url", "og:video:secure_url")
    clips.extend(node.get("src") for node in soup.select("video[src]")[:4])
    clip = None
    for raw in clips:
        clip = _public_mp4(raw, page, explicit_mp4=video_type)
        if clip:
            break
    images = _meta_values(
        soup, "og:image", "og:image:url", "twitter:image", "twitter:image:src"
    )
    for item in _structured_items(soup):
        raw = item.get("image") or item.get("thumbnailUrl")
        if isinstance(raw, list):
            images.extend(raw)
        elif raw:
            images.append(raw)
    for node in soup.select(
        "[data-shot-media] img, .shot-media img, .shot-media-container img, video[poster]"
    )[:12]:
        images.append(node.get("poster") or node.get("src") or node.get("data-src"))

    media_count = 0
    for raw in images:
        if isinstance(raw, dict):
            raw = raw.get("url") or raw.get("contentUrl")
        address = _public_asset(raw, page)
        if not address or address in seen:
            continue
        seen.add(address)
        kind = "video" if clip and media_count == 0 else "photo"
        item = {"url": address, "kind": kind}
        if kind == "video":
            item["clip_url"] = clip
        previews.append(item)
        media_count += 1
        if media_count >= 4:
            break
    if any("userupload" in item["url"] and item["kind"] != "avatar" for item in previews):
        previews = [
            item for item in previews
            if item["kind"] == "avatar" or "userupload" in item["url"]
        ]
    return previews[:5]


class DribbbleAdapter:
    name = "dribbble"

    def match(self, url) -> SocialMatch | None:
        shot = shot_id(url)
        if shot:
            return SocialMatch("dribbble", "post", shot)
        handle = profile_handle(url)
        if handle:
            return SocialMatch("dribbble", "profile", handle)
        return None

    def readers(self, url, match):
        if match.role == "profile":
            canonical = f"https://dribbble.com/{match.key}"
            return [canonical, f"https://r.jina.ai/{canonical}"]
        segment = next(
            (part for part in url.path.split("/") if part and part.lower() != "shots"),
            match.key,
        )
        canonical = f"https://dribbble.com/shots/{segment}"
        oembed = "https://dribbble.com/services/oembed?" + urlencode({"url": canonical})
        # Dribbble answers the official page with a bot challenge. The public
        # reader is the copy that still carries the shot image and caption.
        return [oembed, canonical, f"https://r.jina.ai/{canonical}"]

    def media(self, body, url, match):
        value = _json(body)
        if value is not None:
            return _media_from_json(value, url, match)
        try:
            return _media_from_html(BeautifulSoup(body, "html.parser"), url, match)
        except (TypeError, ValueError):
            return []

    def parse(self, body, url, match):
        from queep.sources import SourceError, clip

        value = _json(body)
        if value is not None:
            name = value.get("author_name") if isinstance(value.get("author_name"), str) else ""
            handle = ""
            for key in ("author_handle", "username"):
                if isinstance(value.get(key), str):
                    handle = value[key].strip().lstrip("@")
                    break
            handle = handle or _profile_from_url(value.get("author_url"))
            raw_text = next(
                (
                    value.get(key)
                    for key in ("description_text", "description", "text")
                    if isinstance(value.get(key), str) and value.get(key).strip()
                ),
                value.get("title") if isinstance(value.get("title"), str) else "",
            )
            text = _plain_text(raw_text)
            if not (name.strip() or handle or text):
                raise SourceError("empty")
            name = name.strip() or handle
        else:
            try:
                soup = BeautifulSoup(body, "html.parser")
            except (TypeError, ValueError) as error:
                raise SourceError("empty") from error
            page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
            og_title = _meta(soup, "og:title", "twitter:title") or page_title
            name, handle = _identity_from_html(soup, match, og_title, url)
            if match.role == "profile":
                text = _profile_bio_from_html(soup)
            else:
                text = _caption_from_html(soup, og_title)
            if not (og_title or name or handle or text):
                raise SourceError("empty")
            name = name.strip() or handle

        if match.role == "profile":
            handle = handle or match.key
            name = name or handle
            return SourceContent(
                content_kind="profile",
                state="read",
                url=str(url),
                title=name[:200],
                text=clip(text, 3000),
                author_name=name[:200],
                author_handle=handle[:32],
                native_title=True,
                fetched_at=datetime.now(UTC),
                limitations=[
                    "Foi lida a bio pública do perfil. Shots, portfólio e estatísticas "
                    "não foram analisados."
                    if text
                    else "O perfil não publicou uma bio pública. Shots, portfólio e "
                    "estatísticas não foram analisados."
                ],
            )
        return SourceContent(
            content_kind="metadata",
            state="read",
            url=str(url),
            title=(name or handle)[:200],
            text=clip(text, 3000),
            author_name=name[:200],
            author_handle=handle[:32],
            native_title=False,
            fetched_at=datetime.now(UTC),
            limitations=[
                "Foram lidos o autor e o texto públicos do shot. Curtidas, visualizações "
                "e comentários não foram analisados. A mídia pública é analisada separadamente."
                if text
                else "Foi lido o autor público do shot; a descrição pode estar indisponível. "
                "Curtidas, visualizações e comentários não foram analisados."
            ],
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
                "Foi lida somente a mídia pública do shot; autor, descrição, curtidas, "
                "visualizações e comentários não estavam disponíveis."
            ],
        )
