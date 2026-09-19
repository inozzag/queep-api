"""Shared social-network match. Adapters own extraction; this is only the dispatch key."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SocialMatch:
    network: str
    role: str  # "post" or "profile"
    key: str
