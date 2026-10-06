"""Per-network social adapters. Register a module here; keep fetch() network-agnostic."""

from queep.social.behance import BehanceAdapter
from queep.social.dribbble import DribbbleAdapter
from queep.social.instagram import InstagramAdapter
from queep.social.pinterest import PinterestAdapter
from queep.social.twitter import TwitterAdapter

ADAPTERS = (
    TwitterAdapter(),
    InstagramAdapter(),
    DribbbleAdapter(),
    BehanceAdapter(),
    PinterestAdapter(),
)


def resolve(url):
    for adapter in ADAPTERS:
        matched = adapter.match(url)
        if matched:
            return adapter, matched
    return None, None


def owner(url):
    """The network that owns this host, even when the path is not a post or profile."""
    for adapter in ADAPTERS:
        if adapter.owns(url):
            return adapter
    return None


def is_profile(url):
    _, matched = resolve(url)
    return bool(matched and matched.role == "profile")
