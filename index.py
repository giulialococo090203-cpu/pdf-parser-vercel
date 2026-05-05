import re
import tempfile
from typing import List, Dict, Any

import pdfplumber
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from parser_common import (
    parse_italian_number,
    clean_description,
    deduplicate_items,
)
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
    return {
        "ok": True,
        "status": "running",
    }


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
                    "preview": text[:2000],
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

        extracted_pages = []

        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                if page_text.strip():
                    extracted_pages.append(page_text)

        return "\n".join(extracted_pages)


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
    section = extract_products_section(normalized)

    lines = [
        line.strip()
        for line in section.split("\n")
        if line and line.strip()
    ]

    results = []
    current_item = None

    for line in lines:
        if should_ignore_line(line):
            continue

        code_match = re.search(r"Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)", line, re.IGNORECASE)
        if code_match and current_item:
            current_item["code"] = code_match.group(1).strip()
            continue

        product = parse_product_line(line)

        if product:
            if current_item:
                results.append(finalize_item(current_item))

            current_item = product
            continue

        if current_item and is_continuation_line(line):
            current_item["description"] = clean_description(
                f'{current_item.get("description", "")} {line}'
            )

    if current_item:
        results.append(finalize_item(current_item))

    return [
        item
        for item in results
        if item.get("description") and float(item.get("quantity", 0) or 0) > 0
    ]


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


def parse_product_line(line: str):
    clean_line = re.sub(r"\s+", " ", str(line or "")).strip()

    patterns = [
        # 1 GRUPPO RITORNO 1 ST 75,98000000 € 75,98 € 22 % -
        r"^(\d+)\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Z]{1,8})\s+(\d+(?:[.,]\d+)?)\s*€?\s+(\d+(?:[.,]\d+)?)\s*€?",

        # 1 GRUPPO RITORNO ST 1 75,98000000 75,98
        r"^(\d+)\s+(.+?)\s+([A-Z]{1,8})\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)",

        # 1 GRUPPO RITORNO 1 ST 75,98000000
        r"^(\d+)\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Z]{1,8})\s+(\d+(?:[.,]\d+)?)",
    ]

    for index, pattern in enumerate(patterns):
        match = re.match(pattern, clean_line, re.IGNORECASE)

        if not match:
            continue

        if index == 1:
            return {
                "rowNumber": match.group(1),
                "description": clean_description(match.group(2)),
                "unit": match.group(3).strip(),
                "quantity": parse_italian_number(match.group(4)),
                "price": parse_italian_number(match.group(5)),
                "total": parse_italian_number(match.group(6)),
                "code": "",
            }

        return {
            "rowNumber": match.group(1),
            "description": clean_description(match.group(2)),
            "quantity": parse_italian_number(match.group(3)),
            "unit": match.group(4).strip(),
            "price": parse_italian_number(match.group(5)),
            "total": parse_italian_number(match.group(6)) if len(match.groups()) >= 6 else 0,
            "code": "",
        }

    return None


def should_ignore_line(line: str) -> bool:
    value = str(line or "").strip()

    ignored_patterns = [
        r"^NR\s+DESCRIZIONE",
        r"^DESCRIZIONE\s+QUANT",
        r"^COD\.?\s*TIPO",
        r"^COD\.?\s*VALORE",
        r"METODO\s+DI\s+PAGAMENTO",
        r"REGIME\s+FISCALE",
        r"DATI\s+AGGIUNTIVI",
        r"RIEPILOGO\s+IVA",
        r"CALCOLO\s+FATTURA",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in ignored_patterns)


def is_continuation_line(line: str) -> bool:
    value = str(line or "").strip()

    if len(value) <= 2:
        return False

    if re.match(r"^\d+\s+", value):
        return False

    if re.match(r"^Cod\.?", value, re.IGNORECASE):
        return False

    if re.search(r"€\s*\d+", value):
        return False

    return True


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