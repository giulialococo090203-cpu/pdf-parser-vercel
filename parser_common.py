import re
from typing import List, Dict, Any


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_italian_number(value: str) -> float:
    text = normalize_spaces(value)
    text = text.replace(".", "").replace(",", ".")
    text = re.sub(r"[^\d.\-]", "", text)

    try:
        return float(text)
    except ValueError:
        return 0.0


def clean_description(value: str) -> str:
    text = normalize_spaces(value)

    # Rimuove blocchi tecnici della fattura elettronica che non sono descrizione materiale
    text = re.sub(r"\bTipo dato:\s*[^,]+,?", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRiferimento testo:\s*[A-Z0-9]+,?", "", text, flags=re.IGNORECASE)

    # Rimuove testo di intestazione/piè pagina che pdfplumber può attaccare alla riga articolo
    text = re.sub(
        r"Copia analogica della fattura elettronica.*?PRODOTTI E SERVIZI",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Fattura Nr\..*?PRODOTTI E SERVIZI",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"Il documento xml originale.*?Entrate\"?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # Rimuove residui tecnici frequenti
    text = re.sub(r"\bPILE\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAEE\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRICAMBIO\b$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRICAMBI\b$", "", text, flags=re.IGNORECASE)

    # Pulizia finale
    text = re.sub(r"\s*,\s*", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[-–—,\s]+", "", text)
    text = re.sub(r"[-–—,\s]+$", "", text)

    return text.strip()


def _round_number(value: Any, digits: int = 6) -> float:
    try:
        return round(float(value or 0), digits)
    except Exception:
        return 0.0


def deduplicate_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplica senza perdere quantità.

    Prima il parser eliminava righe identiche come:
    - 0050 GRUPPO GAS qta 1 prezzo 94,36
    - 0080 GRUPPO GAS qta 1 prezzo 94,36

    Questo era sbagliato perché in fattura sono due righe reali.
    Ora le righe con stesso codice, descrizione, unità, prezzo e marca vengono aggregate:
    quantità = somma quantità
    totale = somma totale
    """
    grouped: Dict[tuple, Dict[str, Any]] = {}

    for item in items or []:
        code = str(item.get("code", "") or "").strip()
        description = clean_description(item.get("description", ""))
        unit = str(item.get("unit", "") or "").strip().upper()
        price = _round_number(item.get("price", 0), 6)
        brand = str(item.get("brand", "") or "").strip()

        key = (
            code.lower(),
            description.lower(),
            unit,
            price,
            brand.lower(),
        )

        quantity = _round_number(item.get("quantity", 0), 6)
        total = _round_number(item.get("total", 0), 6)

        if key not in grouped:
            grouped[key] = {
                **item,
                "code": code,
                "description": description,
                "unit": unit or item.get("unit", "ST"),
                "price": price,
                "quantity": quantity,
                "total": total,
                "brand": brand,
            }
            continue

        grouped[key]["quantity"] = _round_number(
            grouped[key].get("quantity", 0) + quantity,
            6,
        )

        grouped[key]["total"] = _round_number(
            grouped[key].get("total", 0) + total,
            6,
        )

    return list(grouped.values())