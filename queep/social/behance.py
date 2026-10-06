"""Behance projects and profiles. Public oEmbed/HTML only; no login or GraphQL."""

import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from queep.models import SourceContent
from queep.social.types import SocialMatch

HOSTS = {"behance.net", "www.behance.net", "be.net", "www.be.net"}
RESERVED = {
    "about",
    "adobe",
    "api",
    "assets",
    "blog",
    "careers",
    "cloud",
    "copyright",
    "courses",
    "embed",
    "explore",
    "for-you",
    "for_you",
    "galleries",
    "gallery",
    "help",
    "hire",
    "internships",
    "job",
    "joblist",
    "jobs",
    "live",
    "login",
    "messages",
    "misc",
    "notifications",
    "oauth",
    "onboarding",
    "privacy",
    "pro",
    "projects",
    "search",
    "services",
    "settings",
    "signup",
    "talent",
    "terms",
    "v2",
    "v3",
}
SIZE_RANK = {
    "source": 0,
    "max_3840": 1,
    "fs": 2,
    "hd": 3,
    "1400": 4,
    "max_1200": 5,
    "max_808": 6,
    "max_808_webp": 6,
    "disp": 7,
    "1400_webp": 8,
    "max_1200_webp": 9,
    "404_webp": 10,
    "230_webp": 11,
    "202_webp": 12,
    "115_webp": 13,
}


def behance_host(url):
    host = (url.host or "").lower()
    return host in HOSTS or host.endswith(".behance.net")


def project_id(url):
    """Numeric id from /gallery/{id}/slug; never a profile or services path."""
    if not behance_host(url):
        return None
    parts = [part for part in url.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() != "gallery":
        return None
    if not re.fullmatch(r"[0-9]{1,20}", parts[1]):
        return None
    return parts[1]


def profile_handle(url):
    """Designer handle from a one-segment Behance path."""
    if not behance_host(url) or project_id(url):
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


class BehanceAdapter:
    name = "behance"

    def owns(self, url):
        return behance_host(url)

    def match(self, url):
        gallery = project_id(url)
        if gallery:
            return SocialMatch("behance", "post", gallery)
        handle = profile_handle(url)
        if handle:
            return SocialMatch("behance", "profile", handle)
        return None

    def readers(self, url, match):
        if match.role == "profile":
            return [str(url)]
        oembed = "https://www.behance.net/services/oembed?" + urlencode({"url": str(url)})
        embed = f"https://www.behance.net/embed/project/{match.key}"
        return [oembed, embed, str(url)]

    def media(self, body, url, match):
        payload = _payload(body, url, match)
        items = []
        if payload.get("avatar"):
            items.append({"url": payload["avatar"], "kind": "avatar"})
        if match.role == "profile":
            return items[:1]
        used_clip = False
        for slide in payload.get("slides") or []:
            item = dict(slide)
            if item.get("kind") == "video" and item.get("clip_url"):
                if used_clip:
                    item.pop("clip_url", None)
                used_clip = True
            items.append(item)
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
                author_handle=handle[:32],
                native_title=True,
                fetched_at=datetime.now(UTC),
                limitations=[
                    "Foi lida a bio pública do perfil. Projetos, serviços e estatísticas "
                    "não foram analisados."
                    if text
                    else "O perfil não publicou uma bio pública. Projetos, serviços e "
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
            author_handle=handle[:32],
            native_title=False,
            fetched_at=datetime.now(UTC),
            limitations=[_post_limitation(slides, text)],
        )

    def attach_gallery(self, source, media):
        """oEmbed text plus a cover found by a later reader."""
        slides = [item for item in media if item.get("kind") != "avatar"]
        if not slides or source.content_kind != "metadata":
            return source
        return source.model_copy(update={"limitations": [_post_limitation(slides, source.text)]})

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
                "Foi lida somente a mídia pública do projeto; autor, descrição, "
                "comentários e estatísticas não estavam disponíveis."
            ],
        )


def _post_limitation(slides, text):
    has_video = any(item.get("kind") == "video" for item in slides)
    if slides and has_video:
        return (
            "Foi lido o texto público e até 4 mídias. O áudio do vídeo só é analisado "
            "num recorte curto. Comentários e estatísticas não foram analisados."
        )
    if slides:
        return (
            "Foi lido o texto público e até 4 imagens. Comentários e estatísticas "
            "não foram analisados."
        )
    if text:
        return (
            "Foi lida somente a descrição pública do projeto. Áudio, vídeo, imagens "
            "e comentários não foram analisados. Para analisar a mídia, envie o arquivo."
        )
    return "Foi lido o autor público do projeto; o texto completo pode estar indisponível."


def _payload(body, page, match):
    empty = {"author": "", "handle": "", "caption": "", "avatar": None, "slides": []}
    if not body:
        return empty
    try:
        value = json.loads(body)
        if isinstance(value, dict):
            parsed = _from_json(value, page, match)
            if parsed:
                return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        soup = BeautifulSoup(body, "html.parser")
    except (TypeError, ValueError):
        return empty
    store = soup.find(id="beconfig-store_state")
    if store:
        raw = store.string or store.get_text() or ""
        try:
            parsed = _from_json(json.loads(raw), page, match)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if parsed and (parsed["caption"] or parsed["slides"] or parsed["author"]):
            return parsed
    return _from_html(soup, page, match)


def _from_json(value, page, match):
    project = _project_node(value)
    if project:
        return _from_project(project, page, match)
    if match.role == "post" and (
        isinstance(value.get("html"), str) or value.get("provider_name") == "Behance"
    ):
        return _from_oembed(value, page, match)
    return None


def _project_node(value):
    if not isinstance(value, dict):
        return None
    modules = value.get("modules")
    if isinstance(modules, list) and (
        isinstance(value.get("name"), str) or isinstance(value.get("owners"), list)
    ):
        return value
    for key in ("project", "data"):
        nested = value.get(key)
        if isinstance(nested, dict):
            found = _project_node(nested)
            if found:
                return found
    return None


def _from_project(project, page, match):
    owners = project.get("owners") if isinstance(project.get("owners"), list) else []
    author, handle, avatar = "", "", None
    if owners and isinstance(owners[0], dict):
        author, handle, avatar = _owner(owners[0], page)
    elif owners and isinstance(owners[0], str):
        author = owners[0].strip()
    caption = _plain_text(project.get("description") or "")
    slides, seen, ranks = [], {}, {}
    for module in project.get("modules") or []:
        if not isinstance(module, dict):
            continue
        kind = str(module.get("__typename") or module.get("type") or "").lower()
        if "text" in kind:
            text = _plain_text(module.get("text") or module.get("caption") or "")
            if text and len(text) > len(caption):
                caption = text
            continue
        if match.role == "profile":
            continue
        clip = _module_clip(module, page)
        image = _module_image(module, page)
        if clip and image:
            _add_slide(slides, seen, ranks, image, "video", clip)
        elif clip:
            _add_slide(slides, seen, ranks, clip, "video", clip)
        elif image:
            _add_slide(slides, seen, ranks, image, "photo", None)
        if len(slides) >= 4:
            break
    return {
        "author": author or handle,
        "handle": handle,
        "caption": caption,
        "avatar": avatar,
        "slides": slides[:4],
    }


def _from_oembed(value, page, match):
    name = value.get("author_name") if isinstance(value.get("author_name"), str) else ""
    handle = _handle_from_url(value.get("author_url"))
    caption = _plain_text(
        next(
            (
                value.get(key)
                for key in ("description_text", "description", "title")
                if isinstance(value.get(key), str) and value.get(key).strip()
            ),
            "",
        )
    )
    if name and caption.endswith(" - " + name):
        caption = caption[: -len(" - " + name)].strip()
    slides, seen, ranks = [], {}, {}
    clip = _public_mp4(value.get("video_url") or value.get("content_url"), page)
    poster = _media_url(value.get("thumbnail_url"), page)
    if clip and poster:
        _add_slide(slides, seen, ranks, poster, "video", clip)
    elif poster:
        _add_slide(slides, seen, ranks, poster, "photo", None)
    avatar = _media_url(value.get("author_avatar_url") or value.get("thumbnail_url"), page)
    if match.role == "profile":
        return {
            "author": name or handle,
            "handle": handle,
            "caption": caption,
            "avatar": avatar,
            "slides": [],
        }
    if avatar and poster and avatar == poster:
        avatar = None
    return {
        "author": name.strip() or handle,
        "handle": handle,
        "caption": caption,
        "avatar": avatar,
        "slides": slides[:4],
    }


def _from_html(soup, page, match):
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    og_title = _meta(soup, "og:title", "twitter:title") or title
    name = _meta(soup, "og:owners")
    handle = ""
    if match.role == "profile":
        handle = _handle_from_url(_meta(soup, "og:url")) or profile_handle(page) or ""
    else:
        for tag in soup.select("a[href]"):
            parsed = _as_url(tag.get("href"), page)
            candidate = profile_handle(parsed) if parsed else ""
            if candidate:
                handle = candidate
                break
    if not name:
        name = _profile_alt_name(soup)
    if not name:
        name = _name_from_title(og_title, match.role)
    caption = _plain_text(_meta(soup, "og:description", "twitter:description", "description"))
    if match.role == "post" and not caption:
        project = _project_title(og_title)
        if project and project != name:
            caption = project
    if match.role == "post" and name and caption.endswith(" - " + name):
        caption = caption[: -len(" - " + name)].strip()
    avatar = _avatar_from_html(soup, page, match)
    if match.role == "profile":
        return {
            "author": name or handle,
            "handle": handle,
            "caption": caption,
            "avatar": avatar,
            "slides": [],
        }
    slides, seen, ranks = [], {}, {}
    clip = None
    for raw in _meta_values(soup, "og:video", "og:video:url", "og:video:secure_url"):
        clip = _public_mp4(raw, page)
        if clip:
            break
    for node in soup.select("video[src], source[src]"):
        clip = clip or _public_mp4(node.get("src"), page)
        if clip:
            break
    cover = _media_url(_meta(soup, "og:image", "twitter:image", "twitter:image:src"), page)
    if clip and cover:
        _add_slide(slides, seen, ranks, cover, "video", clip)
    elif cover:
        _add_slide(slides, seen, ranks, cover, "photo", None)
    for node in soup.find_all("img"):
        for raw in _img_candidates(node):
            address = _media_url(raw, page)
            if not address or not (_is_module(address) or _is_project_cover(address)):
                continue
            _add_slide(slides, seen, ranks, address, "photo", None)
        if len(slides) >= 4:
            break
    return {
        "author": name or handle,
        "handle": handle,
        "caption": caption,
        "avatar": avatar,
        "slides": slides[:4],
    }


def _owner(owner, page):
    name = owner.get("displayName") or owner.get("display_name") or owner.get("name") or ""
    if not isinstance(name, str):
        name = ""
    handle = owner.get("username") if isinstance(owner.get("username"), str) else ""
    handle = handle or _handle_from_url(owner.get("url"))
    images = owner.get("images")
    avatar = None
    if isinstance(images, dict):
        for key in ("276", "230", "138", "115", "100", "50"):
            avatar = _media_url(images.get(key), page)
            if avatar:
                break
        if not avatar:
            for raw in images.values():
                avatar = _media_url(raw, page)
                if avatar:
                    break
    avatar = avatar or _media_url(owner.get("image") or owner.get("avatar"), page)
    return name.strip(), handle.lstrip("@"), avatar


def _module_image(module, page):
    sizes = module.get("imageSizes")
    if isinstance(sizes, dict) and isinstance(sizes.get("allAvailable"), list):
        ranked = []
        for item in sizes["allAvailable"]:
            if not isinstance(item, dict):
                continue
            address = _media_url(item.get("url"), page)
            if address:
                ranked.append((_size_rank(address), address))
        ranked.sort(key=lambda item: item[0])
        if ranked:
            return ranked[0][1]
    for key in ("src", "url", "displaySrc", "image"):
        raw = module.get(key)
        if isinstance(raw, dict):
            raw = raw.get("url")
        address = _media_url(raw, page)
        if address:
            return address
    return None


def _module_clip(module, page):
    for key in ("videoUrl", "video_url", "src", "url"):
        clip = _public_mp4(module.get(key), page)
        if clip:
            return clip
    embed = module.get("embed")
    if isinstance(embed, str):
        found = re.search(r'src="([^"]+)"', embed)
        if found:
            return _public_mp4(found.group(1), page)
    return None


def _add_slide(slides, seen, ranks, address, kind, clip):
    if not address or kind not in ("photo", "video"):
        return
    key = _slide_key(address)
    rank = _size_rank(address)
    if key in seen:
        index = seen[key]
        if rank >= ranks.get(key, 99):
            return
        slides[index] = _slide(address, kind, clip)
        ranks[key] = rank
        return
    if len(slides) >= 4:
        return
    seen[key] = len(slides)
    ranks[key] = rank
    slides.append(_slide(address, kind, clip))


def _slide(address, kind, clip):
    item = {"url": address, "kind": kind}
    if kind == "video" and clip:
        item["clip_url"] = clip
    return item


def _slide_key(url):
    name = url.split("?", 1)[0].rsplit("/", 1)[-1]
    return name.replace("_webp", "")


def _size_rank(url):
    parts = url.split("?", 1)[0].split("/")
    for part in reversed(parts):
        if part in SIZE_RANK:
            return SIZE_RANK[part]
    return 50


def _avatar_from_html(soup, page, match):
    for node in soup.find_all("img"):
        raw = node.get("src") or node.get("data-src")
        address = _media_url(raw, page)
        if address and "pps.services.adobe.com" in address:
            return address
    if match.role == "profile":
        return None
    return _media_url(_meta(soup, "author:image", "og:profile:image"), page)


def _is_module(url):
    path = url.split("?", 1)[0].lower()
    return "/project_modules/" in path


def _is_project_cover(url):
    path = url.split("?", 1)[0].lower()
    return "/projects/" in path and path.endswith((".png", ".jpg", ".jpeg", ".webp"))


def _img_candidates(node):
    found = []
    for raw in (node.get("src"), node.get("data-src")):
        if isinstance(raw, str) and raw.strip():
            found.append(raw.strip())
    srcset = node.get("srcset")
    if isinstance(srcset, str):
        for part in srcset.split(","):
            bits = part.strip().split()
            if bits:
                found.append(bits[0])
    return found


def _profile_alt_name(soup):
    for node in soup.find_all("img"):
        alt = node.get("alt") if isinstance(node.get("alt"), str) else ""
        found = re.fullmatch(r"(.+?)['\u2019]s profile", alt.strip(), re.I)
        if found and found.group(1).strip():
            return found.group(1).strip()
    return ""


def _project_title(title):
    value = title or ""
    for marker in (" :: Behance", " | Behance", " - Behance", " on Behance"):
        if marker in value:
            value = value.split(marker, 1)[0]
    value = value.strip()
    if " - " in value:
        value = value.split(" - ", 1)[0].strip()
    return value


def _name_from_title(title, role):
    value = title or ""
    for marker in (" :: Behance", " | Behance", " - Behance", " on Behance"):
        if marker in value:
            value = value.split(marker, 1)[0]
            break
    value = value.strip()
    if " - " not in value:
        return value
    left, right = value.rsplit(" - ", 1)
    if role == "profile":
        return left.strip() or right.strip()
    return right.strip() or left.strip()


def _plain_text(value):
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


def _handle_from_url(raw):
    from queep.sources import SourceError, checked_url

    if not isinstance(raw, str):
        return ""
    try:
        parsed = checked_url(raw)
    except SourceError:
        return ""
    return profile_handle(parsed) or ""


def _as_url(raw, page):
    from queep.sources import SourceError, checked_url, public_asset_url

    address = public_asset_url(raw, page) if isinstance(raw, str) else None
    if not address:
        return None
    try:
        return checked_url(address)
    except SourceError:
        return None


def _media_url(raw, page):
    from queep.sources import public_asset_url

    if isinstance(raw, dict):
        raw = raw.get("url")
    return public_asset_url(raw, page)


def _public_mp4(raw, page):
    from queep.sources import SourceError, checked_url

    address = _media_url(raw, page)
    if not address:
        return None
    try:
        parsed = checked_url(address)
    except SourceError:
        return None
    path = parsed.path.lower()
    name = path.rsplit("/", 1)[-1]
    if path.endswith(".mp4") or "video" in name:
        return address
    return None
