"""X/Twitter adapter. Parsing stays in sources; this file only classifies and delegates."""

from queep.social.types import SocialMatch


class TwitterAdapter:
    name = "twitter"

    def owns(self, url):
        host = (url.host or "").lower().rstrip(".")
        return host in {"x.com", "twitter.com", "mobile.twitter.com"} or host.endswith(
            (".twitter.com", ".x.com")
        )

    def match(self, url):
        from queep.sources import tweet_status_id, twitter_profile_handle

        tweet_id = tweet_status_id(url)
        if tweet_id:
            return SocialMatch("twitter", "post", tweet_id)
        handle = twitter_profile_handle(url)
        if handle:
            return SocialMatch("twitter", "profile", handle)
        return None

    def readers(self, url, match):
        from queep.sources import profile_readers, tweet_readers

        if match.role == "profile":
            return profile_readers(url)
        return tweet_readers(url)

    def media(self, body, url, match):
        from queep.sources import profile_avatar_url, tweet_media_previews, with_author_avatar

        if match.role == "profile":
            avatar = profile_avatar_url(body)
            return [{"url": avatar, "kind": "avatar"}] if avatar else []
        return with_author_avatar(body, tweet_media_previews(body) or [])

    def parse(self, body, url, match):
        from queep.sources import profile_metadata, tweet_metadata

        if match.role == "profile":
            return profile_metadata(body, url)
        return tweet_metadata(body, url)

    def fallback(self, url, match, previews):
        from queep.sources import tweet_photo_fallback

        if match.role == "post" and previews:
            return tweet_photo_fallback(url, previews)
        return None
