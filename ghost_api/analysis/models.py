"""The endpoint map: what the analyzer learns from captured traffic."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ParamType = Literal["integer", "number", "boolean", "string"]


@dataclass
class QueryParam:
    name: str
    required: bool  # present in every sample
    constant: bool  # same value in every sample (and seen more than once)
    type: ParamType
    examples: list[str] = field(default_factory=list)
    sensitive: bool = False  # value was redacted, so the real one is unknown

    @property
    def kind(self) -> str:
        if self.constant:
            return "constant"
        return "required" if self.required else "optional"


@dataclass
class PathParam:
    name: str
    type: ParamType
    examples: list[str] = field(default_factory=list)


@dataclass
class Endpoint:
    method: str
    host: str
    path_template: str  # e.g. /api/books/{id}
    path_params: list[PathParam] = field(default_factory=list)
    query_params: list[QueryParam] = field(default_factory=list)
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    sample_count: int = 0
    request_samples: int = 0  # JSON request bodies that fed the request schema
    response_samples: int = 0  # successful JSON responses that fed the response schema
    statuses: list[int] = field(default_factory=list)
    example_urls: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.method} {self.host}{self.path_template}"

    @property
    def label(self) -> str:
        return f"{self.method} {self.path_template}"
