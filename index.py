from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import re
import io
from typing import List, Dict, Any, Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADER_HINTS = [
    "codice", "cod.", "cod prodotto", "cod. prodotto", "articolo",
    "descrizione", "quantita", "quantità", "qta", "q.tà",
    "um", "u.m.", "prezzo", "importo", "totale"
]

STOP_HINTS = [
    "metodo di pagamento",
    "regime fiscale",
    "dati aggiuntivi",
    "riepilogo iva",
    "calcolo fattura",
    "totale documento",
    "totale iva",
    "netto a pagare",
    "iban",
    "swift",
]

IGNORE_DESCRIPTION_HINTS = [
    "addebito trasporto",
    "spesa accessoria",
    "magg trasp",
    "trasporto",
    "contributo ambientale",
]

CODE_VALUE_RE = re.compile(
    r"Cod\.valore:\s*([A-Z0-9\-/\.]+)",
    re.IGNORECASE,
)

# Riga compatta tipo:
# 10 VALVOLA SICUREZZA 1 PCE 44,180 € 44,18 € 22 % -
TEXTUAL_ITEM_RE = re.compile(
    r"""
    ^\s*
    (?P<pos>\d{1,4})\s+
    (?P<desc>.+?)
    \s+
    (?P<qty>\d+(?:[.,]\d+)?)
    \s+
    (?P<um>[A-Z]{1,4})
    \s+
    (?P<price>\d+(?:[.,]\d+)?)
    \s+€
    \s+
    (?P<total>\d+(?:[.,]\d+)?)
    \s+€
    .*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Riga tabellare tipo Bosch multipagina:
# 0010 8-738-728-744 1 7,45 ... 4,95
BOSCH_TABLE_RE = re.compile(
    r"""
    ^\s*
    (?P<pos>\d{4})
    \s+
    (?P<code>[0-9A-Z\-]{6,})
    \s+
    (?P<qty>\d+(?:[.,]\d+)?)
    \s+
    (?P<price>\d+(?:[.,]\d+)?)
    .*?
    (?P<total>\d+(?:[.,]\d+)?)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_italian_number(value: str) -> float:
    text = str(value or "").strip()
    text = text.replace(".", "").replace(",", ".")
    text = re.sub(r"[^\d\.\-]", "", text)
    try:
        return float(text)
    except ValueError:
        return 0.0


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_description(value: str) -> str:
    text = normalize_spaces(value)
    text = re.sub(r"\bRICAMBIO\b$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\bRICAMBI\b$", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"^[-–—\s]+", "", text)
    return text.strip()


def looks_like_header(line: str) -> bool:
    low = normalize_spaces(line).lower()
    matches = sum(1 for hint in HEADER_HINTS if hint in low)
    return matches >= 2


def looks_like_stop_line(line: str) -> bool:
    low = normalize_spaces(line).lower()
    return any(hint in low for hint in STOP_HINTS)


def looks_like_noise(line: str) -> bool:
    low = normalize_spaces(line).lower()

    if not low:
        return True
    if low.startswith("pagina "):
        return True
    if "robert bosch" in low:
        return True
    if "società unipersonale" in low:
        return True
    if "partita iva" in low:
        return True
    if "cl thermoservice" in low:
        return True
    if "www." in low:
        return True
    if "fattura nr." in low:
        return True
    if "nota di debito" in low and "nr." in low:
        return True
    return False


def should_ignore_item(description: str) -> bool:
    low = clean_description(description).lower()
    return any(hint in low for hint in IGNORE_DESCRIPTION_HINTS)


def score_item(item: Dict[str, Any]) -> bool:
    desc = clean_description(item.get("description", ""))
    qty = float(item.get("quantity", 0) or 0)
    price = float(item.get("price", 0) or 0)
    total = float(item.get("total", 0) or 0)
    code = normalize_spaces(item.get("code", ""))

    if not desc:
        return False
    if should_ignore_item(desc):
        return False
    if qty <= 0:
        return False
    if price <= 0 and total <= 0:
        return False
    if code and len(code) < 3:
        return False
    return True


def finalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": normalize_spaces(item.get("code", "")),
        "description": clean_description(item.get("description", "")),
        "quantity": item.get("quantity", 0) or 0,
        "unit": normalize_spaces(item.get("unit", "PZ")) or "PZ",
        "price": item.get("price", 0) or 0,
        "total": item.get("total", 0) or 0,
    }


def extract_text_with_pdfplumber(file_bytes: bytes) -> str:
    pages_text = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages_text.append(text)
    return "\n".join(pages_text)


def split_lines(text: str) -> List[str]:
    text = text.replace("\r", "\n")
    lines = [normalize_spaces(line) for line in text.split("\n")]
    return [line for line in lines if line]


def parse_products_services_block(text: str) -> List[Dict[str, Any]]:
    block_match = re.search(
        r"PRODOTTI E SERVIZI(.*?)(METODO DI PAGAMENTO|REGIME FISCALE|DATI AGGIUNTIVI|RIEPILOGO IVA|CALCOLO FATTURA)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not block_match:
        return []

    block = block_match.group(1)
    lines = split_lines(block)
    return parse_lines_generic(lines)


def parse_lines_generic(lines: List[str]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for line in lines:
        if looks_like_noise(line):
            continue
        if looks_like_header(line):
            continue
        if looks_like_stop_line(line):
            if current and score_item(current):
                results.append(finalize_item(current))
            current = None
            break

        # Bosch tabella multipagina
        bosch_match = BOSCH_TABLE_RE.match(line)
        if bosch_match:
            if current and score_item(current):
                results.append(finalize_item(current))

            current = {
                "code": bosch_match.group("code"),
                "description": "",
                "quantity": parse_italian_number(bosch_match.group("qty")),
                "unit": "PZ",
                "price": parse_italian_number(bosch_match.group("price")),
                "total": parse_italian_number(bosch_match.group("total")),
            }
            continue

        # Riga compatta testuale
        textual_match = TEXTUAL_ITEM_RE.match(line)
        if textual_match:
            if current and score_item(current):
                results.append(finalize_item(current))

            current = {
                "code": "",
                "description": textual_match.group("desc"),
                "quantity": parse_italian_number(textual_match.group("qty")),
                "unit": normalize_spaces(textual_match.group("um")).upper(),
                "price": parse_italian_number(textual_match.group("price")),
                "total": parse_italian_number(textual_match.group("total")),
            }
            continue

        # Riga codice separata
        code_value_match = CODE_VALUE_RE.search(line)
        if code_value_match and current:
            current["code"] = code_value_match.group(1)
            continue

        # Rumore da non appendere alla descrizione
        low = line.lower()
        if any(x in low for x in [
            "d.d.t.", "vs. ordine", "cessione norm.", "documenti correlati",
            "tipo doc.", "numero doc.", "data doc.", "tipo cess. prestazione"
        ]):
            continue

        # Append descrizione su righe spezzate
        if current and len(line) > 2:
            current["description"] = clean_description(
                f"{current.get('description', '')} {line}"
            )

    if current and score_item(current):
        results.append(finalize_item(current))

    return results


def parse_whole_document(text: str) -> List[Dict[str, Any]]:
    return parse_lines_generic(split_lines(text))


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


def parse_invoice_items(text: str) -> List[Dict[str, Any]]:
    candidates = []

    for parser in (parse_products_services_block, parse_whole_document):
        try:
            items = deduplicate_items(parser(text))
            if items:
                candidates.append(items)
        except Exception:
            continue

    if not candidates:
        return []

    candidates.sort(key=len, reverse=True)
    return candidates[0]


@app.get("/")
def root():
    return {"ok": True, "service": "pdf-parser"}


@app.post("/")
async def parse_invoice_pdf(file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="Nessun file ricevuto.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="File vuoto.")

    try:
        text = extract_text_with_pdfplumber(file_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore lettura PDF: {e}")

    if not text.strip():
        raise HTTPException(
            status_code=400,
            detail="Documento senza testo estraibile. Probabile scansione: serve OCR o mappatura manuale."
        )

    rows = parse_invoice_items(text)

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="Nessuna riga articolo riconosciuta nel PDF."
        )

    return {
        "ok": True,
        "fileName": file.filename,
        "rows": rows,
        "matrix": [
            ["Codice", "Descrizione", "Quantità", "UM", "Prezzo", "Marca", "Categoria", "Posizione"],
            *[
                [
                    row.get("code", ""),
                    row.get("description", ""),
                    row.get("quantity", ""),
                    row.get("unit", "PZ"),
                    row.get("price", 0),
                    "",
                    "",
                    "",
                ]
                for row in rows
            ],
        ],
    }