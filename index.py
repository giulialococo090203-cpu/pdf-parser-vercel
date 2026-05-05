import re
import tempfile
from typing import List, Dict, Any

import pdfplumber
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from parser_common import parse_italian_number, clean_description, deduplicate_items
from parser_scan import build_scan_response


app = FastAPI()

ALLOWED_ORIGINS = [
    "https://magazzino-pro.vercel.app",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.options("/{full_path:path}")
async def preflight_handler(full_path: str, request: Request):
    origin = request.headers.get("origin")

    headers = {
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": request.headers.get(
            "access-control-request-headers", "*"
        ),
    }

    if origin in ALLOWED_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin

    return JSONResponse(content={"ok": True}, headers=headers)


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "pdf-parser-python",
        "status": "running",
        "allowed_origins": ALLOWED_ORIGINS,
    }


@app.get("/health")
def health():
    return {"ok": True, "status": "running"}


@app.post("/parse")
async def parse_invoice_pdf(file: UploadFile = File(...)):
    if not file:
        raise HTTPException(status_code=400, detail="Nessun file ricevuto.")

    file_bytes = await file.read()

    if not file_bytes:
        raise HTTPException(status_code=400, detail="File vuoto.")

    filename = file.filename or "documento.pdf"

    try:
        text = extract_text_from_pdf_bytes(file_bytes)

        if not text.strip():
            return build_scan_response(filename)

        rows = extract_invoice_rows(text)
        rows = deduplicate_items(rows)

        if not rows:
            return {
                "ok": False,
                "fileName": filename,
                "error": "Il PDF è stato letto, ma non sono state riconosciute righe articolo utilizzabili.",
                "rows": [],
                "matrix": [],
                "debug": {
                    "textLength": len(text),
                    "preview": text[:3000],
                },
            }

        return {
            "ok": True,
            "fileName": filename,
            "rows": rows,
            "matrix": build_matrix(rows),
            "debug": {
                "textLength": len(text),
                "rowsFound": len(rows),
            },
        }

    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "fileName": filename,
                "error": str(exc) or "Errore interno durante il parsing PDF.",
            },
        )


def extract_text_from_pdf_bytes(file_bytes: bytes) -> str:
    with tempfile.NamedTemporaryFile(delete=True, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp.flush()

        pages = []

        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    pages.append(page_text)

        return "\n".join(pages)


def build_matrix(rows: List[Dict[str, Any]]) -> List[List[Any]]:
    return [
        [
            "Codice",
            "Descrizione",
            "Quantità",
            "UM",
            "Prezzo Netto",
            "Marca",
            "Categoria",
            "Posizione",
        ],
        *[
            [
                row.get("code", ""),
                row.get("description", ""),
                row.get("quantity", ""),
                row.get("unit", "ST"),
                row.get("price", ""),
                "",
                "",
                "",
            ]
            for row in rows
        ],
    ]


def extract_invoice_rows(text: str) -> List[Dict[str, Any]]:
    normalized = normalize_pdf_text(text)

    # FORMATO BOSCH CLASSICO:
    # 0010 8-738-728-744 1 7,45 -30,00%(c) -5,00%(d) 4,95
    # CONTROLLO FIAMMA
    bosch_classic_rows = extract_bosch_classic_rows(normalized)
    if bosch_classic_rows:
        return bosch_classic_rows

    # FORMATO ARUBA / FATTURA ELETTRONICA:
    # 1 GRUPPO RITORNO 1 ST 75,98000000 € 75,98 € 22 % -
    # Cod.tipo: COD_FORNITORE, Cod.valore: 65105322
    return extract_aruba_invoice_rows(normalized)


def extract_aruba_invoice_rows(text: str) -> List[Dict[str, Any]]:
    section = extract_products_section(text)

    lines = [
        line.strip()
        for line in section.split("\n")
        if line and line.strip()
    ]

    results = []
    current_item = None

    for line in lines:
        value = re.sub(r"\s+", " ", line).strip()

        # Codice fornitore della riga precedente
        code_match = re.search(
            r"Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)",
            value,
            re.IGNORECASE,
        )

        if code_match and current_item:
            current_item["code"] = code_match.group(1).strip()
            continue

        # Riga articolo Aruba / fattura elettronica
        product = parse_aruba_product_line(value)

        if product:
            if current_item and is_valid_material(current_item):
                results.append(finalize_item(current_item))

            current_item = product
            continue

        # Righe tecniche/spese da attaccare solo se servono, ma non devono sporcare i materiali
        if current_item and is_accessory_or_transport_line(value):
            current_item["skip"] = True
            continue

        if current_item and is_continuation_line(value):
            current_item["description"] = clean_description(
                f'{current_item.get("description", "")} {value}'
            )

    if current_item and is_valid_material(current_item):
        results.append(finalize_item(current_item))

    return [
        item
        for item in results
        if item.get("description") and float(item.get("quantity", 0) or 0) > 0
    ]


def parse_aruba_product_line(line: str):
    value = re.sub(r"\s+", " ", str(line or "")).strip()

    if not value:
        return None

    # Esclude subito trasporto/spese accessorie
    if re.search(r"\b(addebito\s+trasporto|trasporto|spesa\s+accessoria)\b", value, re.IGNORECASE):
        return None

    patterns = [
        # 1 GRUPPO RITORNO 1 ST 75,98000000 € 75,98 € 22 % -
        r"^(\d{1,5})\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Z]{1,8})\s+(\d+(?:[.,]\d+)?)\s*€?\s+(\d+(?:[.,]\d+)?)\s*€?",

        # 10 VALVOLA SICUREZZA 1 PCE 44,180 € 44,18 € 22 % -
        r"^(\d{1,5})\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Z]{1,8})\s+(\d+(?:[.,]\d+)?)\s*€?\s+(\d+(?:[.,]\d+)?)",

        # fallback senza €
        r"^(\d{1,5})\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Z]{1,8})\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)

        if not match:
            continue

        description = clean_description(match.group(2))

        if not description or is_bad_description(description):
            return None

        return {
            "rowNumber": match.group(1),
            "code": "",
            "description": description,
            "quantity": parse_italian_number(match.group(3)),
            "unit": match.group(4).strip(),
            "price": parse_italian_number(match.group(5)),
            "total": parse_italian_number(match.group(6)),
        }

    return None


def extract_bosch_classic_rows(text: str) -> List[Dict[str, Any]]:
    lines = [
        line.strip()
        for line in str(text or "").split("\n")
        if line and line.strip()
    ]

    results = []
    pending = None

    for line in lines:
        value = re.sub(r"\s+", " ", line).strip()

        # 0010 8-738-728-744 1 7,45 -30,00%(c) -5,00%(d) 4,95
        product_match = re.match(
            r"^(\d{4})\s+([0-9A-Z][0-9A-Z\-./]+)\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)\s+(?:[-+]?\d+(?:[.,]\d+)?%\([a-z]\)\s+)*(?:[-+]?\d+(?:[.,]\d+)?%\s+)*(\d+(?:[.,]\d+)?)$",
            value,
            re.IGNORECASE,
        )

        if product_match:
            pending = {
                "rowNumber": product_match.group(1),
                "code": product_match.group(2),
                "quantity": parse_italian_number(product_match.group(3)),
                "list_price": parse_italian_number(product_match.group(4)),
                "total": parse_italian_number(product_match.group(5)),
                "description": "",
                "unit": "ST",
            }
            continue

        if pending and is_bosch_description_line(value):
            quantity = float(pending.get("quantity", 0) or 0)
            total = float(pending.get("total", 0) or 0)
            price = total / quantity if quantity > 0 else float(pending.get("list_price", 0) or 0)

            item = finalize_item(
                {
                    "code": pending.get("code", ""),
                    "description": clean_description(value),
                    "quantity": quantity,
                    "unit": pending.get("unit", "ST"),
                    "price": price,
                    "total": total,
                }
            )

            if is_valid_material(item):
                results.append(item)

            pending = None

    return results


def is_bosch_description_line(line: str) -> bool:
    value = str(line or "").strip()

    if not value:
        return False

    ignored = [
        r"^RICAMBIO$",
        r"^Fattura$",
        r"^Cod\.Cliente",
        r"^Robert Bosch",
        r"^Via ",
        r"^Dati da indicare",
        r"^Dest\.",
        r"^Fattura presso",
        r"^CL THERMOSERVICE",
        r"^VIA ",
        r"^IT-\d+",
        r"^Pagina ",
        r"^Pos Cod\.",
        r"^Descrizione ",
        r"^Cod\.EAN",
        r"^Partita IVA",
        r"^D\.d\.T\.",
        r"^Vs\. ordine",
        r"^del \d",
        r"^Cessione ",
        r"^ROBERT BOSCH",
        r"^Capitale ",
        r"^C\.C\.I\.A\.A\.",
        r"^Bollo ",
        r"^Pile ",
        r"^BOSCH ",
        r"^\d+[,.]\d+$",
        r"^\d{4}\s+",
    ]

    if any(re.search(pattern, value, re.IGNORECASE) for pattern in ignored):
        return False

    return bool(re.search(r"[A-ZÀ-Ü]", value, re.IGNORECASE))


def normalize_pdf_text(text: str) -> str:
    value = str(text or "")
    value = value.replace("\r", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def extract_products_section(text: str) -> str:
    match = re.search(
        r"PRODOTTI\s+E\s+SERVIZI([\s\S]*?)(METODO\s+DI\s+PAGAMENTO|REGIME\s+FISCALE|DATI\s+AGGIUNTIVI|RIEPILOGO\s+IVA|CALCOLO\s+FATTURA|SCADENZE|TOTALE\s+DOCUMENTO)",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return text


def is_continuation_line(line: str) -> bool:
    value = str(line or "").strip()

    if len(value) <= 2:
        return False

    blocked = [
        r"^Cod\.?",
        r"^Tipo dato:",
        r"^Riferimento testo:",
        r"^METODO DI PAGAMENTO",
        r"^REGIME FISCALE",
        r"^DATI AGGIUNTIVI",
        r"^RIEPILOGO IVA",
        r"^CALCOLO FATTURA",
        r"^Copia analogica",
        r"^Fattura Nr\.",
        r"^\d+\s+",
    ]

    return not any(re.search(pattern, value, re.IGNORECASE) for pattern in blocked)


def is_accessory_or_transport_line(line: str) -> bool:
    return bool(
        re.search(
            r"\b(addebito\s+trasporto|trasporto|spesa\s+accessoria|tipo\s+cess\.\s*prestazione)\b",
            str(line or ""),
            re.IGNORECASE,
        )
    )


def is_bad_description(description: str) -> bool:
    value = str(description or "").strip()

    if not value:
        return True

    bad = [
        r"addebito\s+trasporto",
        r"trasporto",
        r"spesa\s+accessoria",
        r"pagamento",
        r"regime fiscale",
        r"dati aggiuntivi",
        r"riepilogo iva",
        r"calcolo fattura",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in bad)


def is_valid_material(item: Dict[str, Any]) -> bool:
    if item.get("skip"):
        return False

    description = str(item.get("description", "") or "").strip()

    if is_bad_description(description):
        return False

    quantity = safe_number(item.get("quantity", 0))
    price = safe_number(item.get("price", 0))

    return bool(description) and quantity > 0 and price >= 0


def finalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": str(item.get("code", "") or "").strip(),
        "description": clean_description(item.get("description", "")),
        "quantity": safe_number(item.get("quantity", 0)),
        "unit": str(item.get("unit", "ST") or "ST").strip(),
        "price": safe_number(item.get("price", 0)),
        "total": safe_number(item.get("total", 0)),
    }


def safe_number(value) -> float:
    try:
        number = float(value)
        return number if number == number else 0
    except Exception:
        return 0