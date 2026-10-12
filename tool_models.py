"""Strict public schemas; client file references contain bytes via download_url."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OpenAIFile(StrictModel):
    download_url: str = Field(min_length=8, max_length=8192)
    file_id: str = Field(min_length=1, max_length=512)
    mime_type: str = "application/octet-stream"
    file_name: str = "attachment.bin"


class MessageOptions(StrictModel):
    reply_to: int | None = Field(default=None, gt=0)
    topic_id: int | None = Field(default=None, gt=0)
    parse_mode: Literal["plain", "html", "markdown"] = "plain"
    link_preview: bool = False
    silent: bool = False
    schedule_at: str | None = None


class MediaItem(StrictModel):
    file_id: str = Field(min_length=32, max_length=43)
    kind: Literal["document", "photo", "video", "video_note", "voice", "audio", "animation", "sticker"] = "document"
    caption: str = Field(default="", max_length=1024)
    original: bool = True


class TextRun(StrictModel):
    text: str = Field(max_length=32000)
    style: Literal["plain", "bold", "italic", "code", "strike", "underline"] = "plain"
    url: str | None = Field(default=None, max_length=2048)


class ArticleBlock(StrictModel):
    type: Literal["heading", "paragraph", "photo", "list", "quote", "divider"]
    text: str = Field(default="", max_length=32000)
    runs: list[TextRun] = Field(default_factory=list, max_length=300)
    file_id: str | None = Field(default=None, min_length=32, max_length=43)
    items: list[str] = Field(default_factory=list, max_length=100)
    level: Literal[1, 2, 3] = 2


class GroupChange(StrictModel):
    action: Literal["create_group", "create_channel", "set_title", "set_description", "invite", "remove", "set_admin", "ban", "unban", "create_topic"]
    title: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=255)
    user_ids: list[int] = Field(default_factory=list, max_length=20)
    user_id: int | None = Field(default=None, gt=0)
    admin_rights: list[Literal["change_info", "post_messages", "edit_messages", "delete_messages", "ban_users", "invite_users", "pin_messages", "add_admins", "manage_call", "manage_topics"]] = Field(default_factory=list)


class ContactChange(StrictModel):
    action: Literal["add", "update", "delete"]
    user_id: int | None = Field(default=None, gt=0)
    phone: str = Field(default="", max_length=32)
    first_name: str = Field(default="", max_length=64)
    last_name: str = Field(default="", max_length=64)


class StoryLink(StrictModel):
    url: str = Field(min_length=8,max_length=2048)
    x: float = Field(default=50,ge=0,le=100)
    y: float = Field(default=85,ge=0,le=100)
    width: float = Field(default=50,gt=0,le=100)
    height: float = Field(default=12,gt=0,le=100)
    rotation: float = Field(default=0,ge=-360,le=360)


class StoryOptions(StrictModel):
    audience: Literal["contacts", "close_friends", "selected", "everyone"] = "contacts"
    selected_user_ids: list[int] = Field(default_factory=list, max_length=100)
    period_seconds: Literal[21600, 43200, 86400, 172800] = 86400
    pinned: bool = False
    noforwards: bool = False
    links: list[StoryLink] = Field(default_factory=list,max_length=5)
