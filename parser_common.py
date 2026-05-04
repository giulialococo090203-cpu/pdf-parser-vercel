import re
from typing import List, Dict, Any

def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()

def parse_italian_number(value: str) -> float:
    text = normalize_spaces(value)
    text = text.replace(".", "").replace(",", ".")
    text = re.sub(r"[^\d\.\-]", "", text)
    try:
        return float(text)
    except ValueError:
        return 0.0

def clean_description(value: str) -> str:
    text = normalize_spaces(value)
    text = re.sub(r"^[-–—\s]+", "", text)
    text = re.sub(r"\bRICAMBIO\b$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\bRICAMBI\b$", "", text, flags=re.IGNORECASE).strip()
    return text.strip()

def deduplicate_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    output = []

    for item in items:
        key = (
            item.get("code", ""),
            item.get("description", ""),
            float(item.get("quantity", 0) or 0),
            item.get("unit", ""),
            float(item.get("price", 0) or 0),
            float(item.get("total", 0) or 0),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)

    return output