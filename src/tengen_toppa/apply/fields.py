"""Form field model shared by the DOM filler, the Q&A engine and the Claude browser agent."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

FieldKind = Literal[
    "text", "email", "tel", "url", "number", "textarea", "select", "combobox", "radio", "checkbox", "file", "date"
]


@dataclass
class FormField:
    key: str  # data-tt-id assigned in the DOM (or the question text for agent-reported fields)
    label: str
    kind: FieldKind
    required: bool = False
    options: list[str] = field(default_factory=list)
    name: str = ""
    value: str = ""  # current value in the form

    @property
    def norm_label(self) -> str:
        return normalize_question(self.label)


def normalize_question(text: str) -> str:
    text = text.lower().replace("*", " ")
    text = re.sub(r"\(required\)|\(optional\)", " ", text)
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def pick_option(options: list[str], answer: str) -> str | None:
    """Map a free answer ('Yes', '30 days', 'India') onto one of the form's options."""
    if not options:
        return answer
    ans = normalize_question(answer)
    norm = [(o, normalize_question(o)) for o in options if normalize_question(o) not in ("", "select", "please select")]
    for o, n in norm:
        if n == ans:
            return o
    if ans in ("yes", "no"):
        for o, n in norm:
            if n.split(" ")[0] == ans:
                return o
    for o, n in norm:
        if ans and (ans in n or n in ans):
            return o
    decline = ("decline", "prefer not", "do not wish", "don t wish", "not want to answer", "i don t want")
    if any(d in ans for d in decline):
        for o, n in norm:
            if any(d in n for d in decline):
                return o
    return None
