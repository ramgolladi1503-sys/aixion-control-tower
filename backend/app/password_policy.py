from __future__ import annotations

import string

PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


def validate_password_policy(password: str, email: str | None = None) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters long")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters long")
    if password != password.strip():
        raise ValueError("Password must not start or end with whitespace")
    if any(character.isspace() for character in password):
        raise ValueError("Password must not contain whitespace")

    lowered = password.lower()
    if len(set(lowered)) < 5:
        raise ValueError("Password must not rely on repeated characters")
    if email:
        local_part = email.lower().split("@", 1)[0].strip()
        if local_part and len(local_part) >= 4 and local_part in lowered:
            raise ValueError("Password must not contain the email username")

    has_lower = any(character in string.ascii_lowercase for character in password)
    has_upper = any(character in string.ascii_uppercase for character in password)
    has_digit = any(character in string.digits for character in password)
    has_symbol = any(character in string.punctuation for character in password)
    if not has_lower or not has_upper or not has_digit or not has_symbol:
        raise ValueError("Password must include lowercase, uppercase, number, and symbol")

    if _contains_obvious_sequence(lowered):
        raise ValueError("Password must not contain obvious keyboard or numeric sequences")
    if _contains_product_word(lowered):
        raise ValueError("Password must not contain obvious product or account words")

    return password


def _contains_obvious_sequence(value: str, window: int = 6) -> bool:
    compact = "".join(character for character in value if character.isalnum())
    if len(compact) < window:
        return False
    for index in range(0, len(compact) - window + 1):
        chunk = compact[index : index + window]
        code_points = [ord(character) for character in chunk]
        if all(right - left == 1 for left, right in zip(code_points, code_points[1:])):
            return True
        if all(left - right == 1 for left, right in zip(code_points, code_points[1:])):
            return True
    return False


def _contains_product_word(value: str) -> bool:
    compact = "".join(character for character in value if character.isalnum())
    return any(term in compact for term in ("aixion", "controltower", "approval", "account"))
