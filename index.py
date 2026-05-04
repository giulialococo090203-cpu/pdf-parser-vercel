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

PARSER_VERSION = "generic-v2"

STOP_HINTS = [
    "metodo di pagamento",
    "regime fiscale",
    "dati aggiuntivi",
    "riepilogo iva",
    "calcolo fattura",
    "totale documento",
    "totale iva",
    "netto a pagare",
]

IGNORE_DESCRIPTION_HINTS = [
    "addebito trasporto",
    "spesa accessoria",
    "magg trasp",
    "trasporto",
    "contributo ambientale",
]

CODE_VALUE_RE = re.compile(r"Cod\.valore:\s*([A-Z0-9\-/\.]+)", re.IGNORECASE)

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

PRODUCTS_BLOCK_RE = re.compile(
    r"PRODOTTI E SERVIZI(.*?)(METODO DI PAGAMENTO|REGIME FISCALE|DATI AGGIUNTIVI|RIEPILOGO IVA|CALCOLO FATTURA)",
    re.IGNORECASE | re.DOTALL,
)


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


def should_ignore_item(description: str) -> bool:
    low = clean_description(description).lower()
    return any(h in low for h in IGNORE_DESCRIPTION_HINTS)


def split_lines(text: str) -> List[str]:
    text = text.replace("\r", "\n")
    return [normalize_spaces(line) for line in text.split("\n") if normalize_spaces(line)]


def extract_text_with_pdfplumber(file_bytes: bytes) -> str:
    pages_text = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages_text.append(text)
    return "\n".join(pages_text)


def score_item(item: Dict[str, Any]) -> bool:
    desc = clean_description(item.get("description", ""))
    qty = float(item.get("quantity", 0) or 0)
    price = float(item.get("price", 0) or 0)
    total = float(item.get("total", 0) or 0)

    if not desc:
        return False
    if should_ignore_item(desc):
        return False
    if qty <= 0:
        return False
    if price <= 0 and total <= 0:
        return False
    return True


def finalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": normalize_spaces(item.get("code", "")),
        "description": clean_description(item.get("description", "")),
        "quantity": float(item.get("quantity", 0) or 0),
        "unit": normalize_spaces(item.get("unit", "PZ")) or "PZ",
        "price": float(item.get("price", 0) or 0),
        "total": float(item.get("total", 0) or 0),
    }


def looks_like_stop_line(line: str) -> bool:
    low = normalize_spaces(line).lower()
    return any(h in low for h in STOP_HINTS)


def parse_bosch_tabular_line(line: str) -> Optional[Dict[str, Any]]:
    m = BOSCH_TABLE_RE.match(line)
    if not m:
        return None

    return {
        "code": m.group("code"),
        "description": "",
        "quantity": parse_italian_number(m.group("qty")),
        "unit": "PZ",
        "price": parse_italian_number(m.group("price")),
        "total": parse_italian_number(m.group("total")),
    }


def parse_textual_item_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Gestisce righe tipo:
    10 VALVOLA SICUREZZA 1 PCE 44,180 € 44,18 € 22 % -
    20 KIT CONVERSIONE GPL BALCONY GB122-24 K H 2 PCE 38,725 € 77,45 € 22 % -
    """
    original = normalize_spaces(line)
    if not original:
        return None

    # deve iniziare con numero riga articolo
    if not re.match(r"^\d{1,4}\s+", original):
        return None

    # taglia via l'eventuale coda IVA / natura dopo il secondo €
    # teniamo comunque tutta la riga per il parsing da sinistra/destra
    parts = original.split()
    if len(parts) < 6:
        return None

    pos = parts[0]
    rest = parts[1:]

    # cerchiamo da destra i token utili: total, €, price, €, um, qty
    # esempio:
    # desc .... 1 PCE 44,180 € 44,18 € 22 % -
    # noi vogliamo:
    # qty = token prima di UM
    # um = PCE
    # price = token prima del primo €
    # total = token prima del secondo €
    euro_positions = [i for i, tok in enumerate(rest) if tok == "€"]
    if len(euro_positions) < 2:
        return None

    first_euro = euro_positions[0]
    second_euro = euro_positions[1]

    if first_euro < 2:
        return None
    if second_euro < 1:
        return None

    price_token = rest[first_euro - 1]
    total_token = rest[second_euro - 1]

    # prima del prezzo ci aspettiamo ... qty um price
    if first_euro < 3:
        return None

    um_token = rest[first_euro - 2]
    qty_token = rest[first_euro - 3]

    desc_tokens = rest[: first_euro - 3]
    desc = " ".join(desc_tokens).strip()

    if not desc:
        return None

    return {
        "code": "",
        "description": desc,
        "quantity": parse_italian_number(qty_token),
        "unit": normalize_spaces(um_token).upper(),
        "price": parse_italian_number(price_token),
        "total": parse_italian_number(total_token),
    }


def parse_products_services_block(text: str) -> List[Dict[str, Any]]:
    match = PRODUCTS_BLOCK_RE.search(text)
    if not match:
        return []

    block = match.group(1)
    lines = split_lines(block)

    results: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for line in lines:
        low = line.lower()

        if looks_like_stop_line(line):
            if current and score_item(current):
                results.append(finalize_item(current))
            current = None
            break

        # salta intestazione tabella
        if "nr descrizione quantita" in low or "nr descrizione quantità" in low:
            continue

        # prima prova Bosch tabellare multipagina
        item = parse_bosch_tabular_line(line)
        if item:
            if current and score_item(current):
                results.append(finalize_item(current))
            current = item
            continue

        # poi prova righe testuali compatte
        item = parse_textual_item_line(line)
        if item:
            if current and score_item(current):
                results.append(finalize_item(current))
            current = item
            continue

        # codice su riga separata
        code_match = CODE_VALUE_RE.search(line)
        if code_match and current:
            current["code"] = code_match.group(1)
            continue

        # righe di rumore da non aggiungere
        if any(x in low for x in [
            "cod.tipo:",
            "tipo cess. prestazione",
            "dati ordine",
            "dati ddt",
            "causale documento",
            "documenti correlati",
        ]):
            continue

        # descrizione spezzata su più righe
        if current and len(line) > 2:
            current["description"] = clean_description(
                f"{current.get('description', '')} {line}"
            )

    if current and score_item(current):
        results.append(finalize_item(current))

    return results


def parse_whole_document(text: str) -> List[Dict[str, Any]]:
    lines = split_lines(text)

    results: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for line in lines:
        low = line.lower()

        if looks_like_stop_line(line):
            if current and score_item(current):
                results.append(finalize_item(current))
            current = None
            continue

        if any(x in low for x in [
            "robert bosch",
            "società unipersonale",
            "pagina ",
            "partita iva",
            "cl thermoservice",
            "iban",
            "swift",
            "www.",
        ]):
            continue

        item = parse_bosch_tabular_line(line)
        if item:
            if current and score_item(current):
                results.append(finalize_item(current))
            current = item
            continue

        item = parse_textual_item_line(line)
        if item:
            if current and score_item(current):
                results.append(finalize_item(current))
            current = item
            continue

        code_match = CODE_VALUE_RE.search(line)
        if code_match and current:
            current["code"] = code_match.group(1)
            continue

        if any(x in low for x in [
            "d.d.t.",
            "vs. ordine",
            "cessione norm.",
            "tipo cess. prestazione",
            "cod.tipo:",
            "documenti correlati",
            "dati ordine",
            "dati ddt",
        ]):
            continue

        if current and len(line) > 2:
            current["description"] = clean_description(
                f"{current.get('description', '')} {line}"
            )

    if current and score_item(current):
        results.append(finalize_item(current))

    return results


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
    return {
        "ok": True,
        "service": "pdf-parser",
        "version": PARSER_VERSION,
    }


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