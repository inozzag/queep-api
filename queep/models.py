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
