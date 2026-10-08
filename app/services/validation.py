"""Per-recipient validation.

Each recipient is validated independently so that one bad row produces a
clear, row-specific error instead of rejecting the entire batch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.schemas import RecipientError, RecipientIn


@dataclass
class ValidationResult:
    valid: list[tuple[int, RecipientIn]] = field(default_factory=list)
    invalid: list[RecipientError] = field(default_factory=list)


def _format_errors(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "recipient"
        msg = err["msg"].removeprefix("Value error, ")
        messages.append(f"{loc}: {msg}")
    return messages


def validate_recipients(raw_recipients: list[Any]) -> ValidationResult:
    """Split raw recipient dicts into valid and invalid rows.

    Rules:
      * schema rules from ``RecipientIn`` (name present/non-blank, valid email, ...)
      * an email may appear only once per job - later duplicates are invalid,
        so a recipient never receives two identical certificates.
    """
    result = ValidationResult()
    seen_emails: dict[str, int] = {}

    for index, raw in enumerate(raw_recipients):
        try:
            recipient = RecipientIn.model_validate(raw)
        except ValidationError as exc:
            result.invalid.append(
                RecipientError(row_index=index, input=raw, errors=_format_errors(exc))
            )
            continue

        if recipient.email in seen_emails:
            result.invalid.append(
                RecipientError(
                    row_index=index,
                    input=raw,
                    errors=[f"email: duplicate of recipient at row {seen_emails[recipient.email]}"],
                )
            )
            continue

        seen_emails[recipient.email] = index
        result.valid.append((index, recipient))

    return result
