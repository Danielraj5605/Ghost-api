"""The actions the agent can take, as one flat JSON object per step.

A flat model (rather than one model per tool) is easier for small free models to fill
in correctly. Cross-field rules live in a validator, so the LLM gateway's repair loop
explains mistakes back to the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ActionName = Literal[
    "click", "type", "scroll", "navigate", "go_back", "call_endpoint", "ask_user", "finish"
]

MAX_TYPED_CHARS = 200


class Decision(BaseModel):
    thought: str = Field(description="One sentence: why this action helps reach the goal.")
    action: ActionName
    element_id: int | None = Field(None, description="Required for click and type.")
    text: str | None = Field(None, description="Required for type: what to enter.")
    submit: bool = Field(False, description="For type: press Enter after typing.")
    direction: Literal["down", "up", "bottom"] | None = Field(
        None, description="For scroll. 'bottom' jumps to the end of the page."
    )
    url: str | None = Field(None, description="Required for navigate: a URL on this site.")
    endpoint: str | None = Field(
        None, description="Required for call_endpoint: an endpoint exactly as listed."
    )
    params: dict[str, str] | None = Field(
        None, description='For call_endpoint: parameters to change, e.g. {"page": "2"}.'
    )
    question: str | None = Field(None, description="Required for ask_user.")
    goal_covered: bool = Field(
        False, description="True only if the endpoints found so far cover every part of the goal."
    )
    missing: str = Field("", description="Which part of the goal still has no endpoint.")

    @model_validator(mode="after")
    def _check_arguments(self) -> Decision:
        if self.action in ("click", "type") and self.element_id is None:
            raise ValueError(f"'{self.action}' needs element_id")
        if self.action == "type":
            if not self.text:
                raise ValueError("'type' needs text")
            if len(self.text) > MAX_TYPED_CHARS:
                raise ValueError(f"text must be at most {MAX_TYPED_CHARS} characters")
        if self.action == "navigate" and not self.url:
            raise ValueError("'navigate' needs url")
        if self.action == "call_endpoint" and not self.endpoint:
            raise ValueError("'call_endpoint' needs endpoint")
        if self.action == "ask_user" and not self.question:
            raise ValueError("'ask_user' needs question")
        return self

    def args(self) -> dict[str, object]:
        """Only the arguments that matter for this action (for logs and history)."""
        keys = {
            "click": ("element_id",),
            "type": ("element_id", "text", "submit"),
            "scroll": ("direction",),
            "navigate": ("url",),
            "go_back": (),
            "call_endpoint": ("endpoint", "params"),
            "ask_user": ("question",),
            "finish": ("goal_covered", "missing"),
        }[self.action]
        return {k: getattr(self, k) for k in keys}


# Element kinds that accept typed text.
TYPEABLE_PREFIXES = ("input:", "textarea", "searchbox", "combobox", "textbox", "clickable")


def is_typeable(kind: str) -> bool:
    if kind.startswith("input:"):
        return kind.split(":", 1)[1] in {
            "text", "search", "email", "url", "tel", "number", "date", "",
        }  # fmt: skip
    return kind.startswith(TYPEABLE_PREFIXES)
