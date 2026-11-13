"""Scrub personal data from error text before it is stored . Emails and phone numbers are replaced for every project; an app that knows names scrubs them
itself (its own crash reporter). The user's own notes are never scrubbed: they stay exactly as typed.
"""
from __future__ import annotations

import re

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(
    r"(?<![\w:.-])(?!\d{4}-\d{2}-\d{2}(?!\d))"  # not a date
    r"(?:\+\d{1,3}[ -]?)?\(?\d{2,4}\)?[ -]\d{2,4}[ -]\d{2,4}(?:[ -]\d{2,4})?"
    r"(?![\w:.-])"
)


ENCODED_EMAIL = re.compile(r"[\w.+-]+(?:%40|%2540)[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)


def scrub(text: str) -> str:
    """Replace email addresses (also URL-encoded ones) and phone numbers with <email> and <phone>."""
    return PHONE.sub("<phone>", EMAIL.sub("<email>", ENCODED_EMAIL.sub("<email>", text or "")))
