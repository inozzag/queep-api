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


class Interpretation(BaseModel):
    summary: str = Field(max_length=4000)
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


class Reference(BaseModel):
    id: str
    kind: Literal["note", "link", "image"]
    title: str
    text: str
    url: str | None
    status: Literal["queued", "processing", "ready", "error"]
    revision: int
    created_at: datetime
    updated_at: datetime
    analysis: Interpretation | None = None
    image: ImageInfo | None = None


class Library(BaseModel):
    items: list[Reference]
    next_cursor: str | None = None


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=2, max_length=500)


class SearchResults(BaseModel):
    items: list[Reference]
    has_pending: bool
    candidate_limit: int = 10


class RetryAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
