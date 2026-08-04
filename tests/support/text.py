from __future__ import annotations


def normalize_text(text: str) -> str:
    normalized = text.strip()
    normalized = normalized.strip('"\'`。.!?！？：: ')
    return " ".join(normalized.split())
