import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class Capture(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["note", "link"]
    title: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=30000)
    url: HttpUrl | None = None
    space_ids: list[UUID] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def validate_content(self):
        if self.kind == "note" and (not self.text or self.url):
            raise ValueError("A note requires text and cannot have a URL")
        if self.kind == "link" and self.url is None:
            raise ValueError("A link requires an HTTP(S) URL")
        if self.url and (self.url.username or self.url.password):
            raise ValueError("URLs must not contain credentials")
        if self.url and len(str(self.url)) > 4096:
            raise ValueError("URL too long")
        return self


class Edit(Capture):
    revision: int = Field(ge=1)
    mutation_id: UUID


class MetadataEdit(BaseModel):
    """Title and notes of an uploaded image or media file; the file itself never changes."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["image", "audio", "video", "link"]
    title: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=30000)
    revision: int = Field(ge=1)
    mutation_id: UUID


class Interpretation(BaseModel):
    summary: str = Field(max_length=4000)
    headline: str = Field(default="", max_length=80)
    portrait: str = Field(default="", max_length=500)
    topics: list[str] = Field(default_factory=list, max_length=30)
    entities: list[str] = Field(default_factory=list, max_length=30)
    usefulness: list[str] = Field(default_factory=list, max_length=20)
    appearance: list[str] = Field(default_factory=list, max_length=20)
    coverage: Literal["full", "partial"]
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("topics", "entities", "usefulness", "appearance", "limitations")
    @classmethod
    def bounded_entries(cls, value):
        if any(len(entry) > 500 for entry in value):
            raise ValueError("Interpretation entry too long")
        return value


class ImageInfo(BaseModel):
    sha256: str
    content_type: Literal["image/jpeg", "image/png"]
    width: int
    height: int
    byte_count: int
    preview_kind: Literal["photo", "video"] = "photo"


class SourceContent(BaseModel):
    content_kind: Literal["page", "metadata", "profile"] = "page"
    state: Literal["read", "unavailable"]
    url: str = Field(max_length=4096)
    title: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=12000)
    author_name: str = Field(default="", max_length=200)
    author_handle: str = Field(default="", max_length=32)
    native_title: bool = False
    fetched_at: datetime
    truncated: bool = False
    limitations: list[str] = Field(default_factory=list, max_length=4)


class MediaInfo(BaseModel):
    sha256: str
    content_type: Literal["audio/mp4", "video/mp4"]
    duration_seconds: float = Field(gt=0, le=301)
    byte_count: int
    has_audio: bool
    width: int | None = None
    height: int | None = None


class Reference(BaseModel):
    id: str
    kind: Literal["note", "link", "image", "audio", "video"]
    title: str
    text: str
    url: str | None
    status: Literal["queued", "processing", "ready", "error"]
    revision: int
    created_at: datetime
    updated_at: datetime
    space_ids: list[str] = Field(default_factory=list)
    archived_at: datetime | None = None
    analysis: Interpretation | None = None
    image: ImageInfo | None = None
    images: list[ImageInfo] | None = Field(default=None, max_length=4)
    author_image: ImageInfo | None = None
    source: SourceContent | None = None
    media: MediaInfo | None = None
    transcript: str | None = Field(default=None, max_length=18000)


class Library(BaseModel):
    items: list[Reference]
    next_cursor: str | None = None


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=500)
    space_id: UUID | None = None


class SearchResults(BaseModel):
    items: list[Reference]
    has_pending: bool
    candidate_limit: int = 10


class ArchiveEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    archived: bool


class BatchArchive(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[UUID] = Field(min_length=1, max_length=50)
    archived: bool


class RetryAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)


_BR_AREA_CODES = {
    "11", "12", "13", "14", "15", "16", "17", "18", "19",
    "21", "22", "24", "27", "28",
    "31", "32", "33", "34", "35", "37", "38",
    "41", "42", "43", "44", "45", "46", "47", "48", "49",
    "51", "53", "54", "55",
    "61", "62", "63", "64", "65", "66", "67", "68", "69",
    "71", "73", "74", "75", "77", "79",
    "81", "82", "83", "84", "85", "86", "87", "88", "89",
    "91", "92", "93", "94", "95", "96", "97", "98", "99",
}
# Longest dial codes first so 351 matches before a shorter prefix.
_DIAL_LENGTHS = (
    ("971", 9, 9), ("598", 8, 8), ("595", 9, 9), ("593", 9, 9), ("591", 8, 8),
    ("358", 9, 9), ("353", 9, 9), ("351", 9, 9), ("258", 9, 9), ("245", 7, 7),
    ("244", 9, 9), ("238", 7, 7), ("972", 9, 9),
    ("90", 10, 10), ("86", 11, 11), ("82", 9, 10), ("81", 10, 10), ("64", 8, 10),
    ("61", 9, 9), ("58", 10, 10), ("57", 10, 10), ("56", 9, 9), ("55", 10, 11),
    ("54", 10, 10), ("52", 10, 10), ("51", 9, 9), ("49", 10, 11), ("48", 9, 9),
    ("47", 8, 8), ("46", 9, 9), ("45", 8, 8), ("44", 10, 10), ("43", 10, 10),
    ("41", 9, 9), ("39", 9, 10), ("34", 9, 9), ("33", 9, 9), ("32", 9, 9),
    ("31", 9, 9), ("27", 9, 9), ("1", 10, 10),
)


def _valid_brazil_national(national: str) -> bool:
    if len(national) not in (10, 11) or national[:2] not in _BR_AREA_CODES:
        return False
    subscriber = national[2:]
    if len(subscriber) == 9:
        return subscriber.startswith("9")
    return subscriber[:1] in "2345"


def _normalize_onboarding_phone(value: str) -> str:
    raw = value.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
        raw = "+" + digits
    if raw.startswith("+"):
        for dial, low, high in _DIAL_LENGTHS:
            if not digits.startswith(dial):
                continue
            national = digits[len(dial):]
            if not low <= len(national) <= high:
                continue
            if dial == "55" and not _valid_brazil_national(national):
                continue
            return "+" + digits
        raise ValueError("Informe um telefone válido")
    if _valid_brazil_national(digits):
        return "+55" + digits
    raise ValueError("Informe um telefone válido")


class OnboardingProfile(BaseModel):
    """Answers collected before the first save. The phone stays on the account document."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    phone: str
    source: Literal[
        "anuncio", "instagram", "tiktok", "twitter", "youtube", "google",
        "linkedin", "amigo", "ai_chat", "podcast", "app_store",
    ]
    work: Literal[
        "design", "marketing", "conteudo", "social", "fotografia", "publicidade",
        "produto", "empreendedor", "educacao", "outro",
    ]
    work_detail: str | None = None

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return _normalize_onboarding_phone(value)

    @model_validator(mode="after")
    def normalize_work_detail(self):
        detail = (self.work_detail or "").strip()
        if self.work != "outro":
            self.work_detail = None
            return self
        if not 2 <= len(detail) <= 40:
            raise ValueError("Diga com o que você trabalha")
        self.work_detail = detail
        return self


class OnboardingRecord(OnboardingProfile):
    completed_at: datetime


class SpaceName(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=40)


class SpaceEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=40)
    sort_index: int | None = Field(default=None, ge=0, le=39)


class Space(BaseModel):
    id: str
    name: str
    sort_index: int
    created_at: datetime
    updated_at: datetime
    reference_count: int = 0


class SpaceMembership(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add: list[UUID] = Field(default_factory=list, max_length=40)
    remove: list[UUID] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def disjoint(self):
        if set(self.add) & set(self.remove):
            raise ValueError("Um Space não pode ser adicionado e removido ao mesmo tempo")
        return self
