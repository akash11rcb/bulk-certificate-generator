"""Unit tests for per-recipient validation rules."""

from app.services.validation import validate_recipients


def test_valid_recipients_pass_and_are_normalised():
    result = validate_recipients(
        [{"name": "  Ada   Lovelace ", "email": "ADA@Example.com", "extra": "ignored"}]
    )
    assert not result.invalid
    index, recipient = result.valid[0]
    assert index == 0
    assert recipient.name == "Ada Lovelace"  # trimmed + whitespace collapsed
    assert recipient.email == "ada@example.com"  # lower-cased


def test_each_invalid_row_is_reported_with_its_index_and_reason():
    result = validate_recipients(
        [
            {"name": "Valid Person", "email": "valid@example.com"},
            {"name": "", "email": "blank@example.com"},
            {"name": "Bad Email", "email": "not-an-email"},
            {"email": "missing-name@example.com"},
            {"name": "12345", "email": "digits@example.com"},
            "just a string",
            {"name": "Tab\x00Char", "email": "ctrl@example.com"},
        ]
    )
    assert [i for i, _ in result.valid] == [0]
    errors = {e.row_index: " ".join(e.errors) for e in result.invalid}
    assert set(errors) == {1, 2, 3, 4, 5, 6}
    assert "name" in errors[1]
    assert "email" in errors[2]
    assert "name" in errors[3] and "required" in errors[3].lower()
    assert "letter" in errors[4]
    assert "dictionary" in errors[5].lower() or "object" in errors[5].lower()
    assert "control" in errors[6]


def test_duplicate_emails_are_rejected_case_insensitively():
    result = validate_recipients(
        [
            {"name": "First", "email": "same@example.com"},
            {"name": "Second", "email": "SAME@example.com"},
        ]
    )
    assert [i for i, _ in result.valid] == [0]
    assert result.invalid[0].row_index == 1
    assert "duplicate of recipient at row 0" in result.invalid[0].errors[0]


def test_name_length_limit():
    result = validate_recipients([{"name": "A" * 101, "email": "long@example.com"}])
    assert result.invalid and "100" in result.invalid[0].errors[0]
