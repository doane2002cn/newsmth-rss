from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Post:
    author: str
    position: str
    body_html: str
    post_id: str = ""
    date: str = ""
    likes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArticlePage:
    board: str
    topic: str
    title: str
    page: int
    total_posts: int
    total_pages: int
    posts: list[Post]
    fetched_at: int

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["posts"] = [post.to_dict() for post in self.posts]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ArticlePage":
        return cls(
            board=str(data["board"]),
            topic=str(data["topic"]),
            title=str(data["title"]),
            page=int(data["page"]),
            total_posts=int(data["total_posts"]),
            total_pages=int(data["total_pages"]),
            posts=[Post(**post) for post in data.get("posts", [])],
            fetched_at=int(data["fetched_at"]),
        )
