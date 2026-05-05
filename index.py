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

# CORS aperto: il parser non usa cookie/sessioni, serve solo a ricevere il PDF dal frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)


@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        return JSONResponse(
            content={"ok": True},
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Max-Age": "86400",
            },
        )

    response = await call_next(request)

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "*"

    return response


@app.options("/{full_path:path}")
async def preflight_handler(full_path: str, request: Request):
    return JSONResponse(
        content={"ok": True},
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": request.headers.get(
                "access-control-request-headers", "*"
            ),
            "Access-Control-Max-Age": "86400",
        },
    )


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "pdf-parser-python",
        "status": "running",
        "allowed_origins": ["*"],
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
        extracted = extract_text_from_pdf_bytes(file_bytes)
        text = extracted.get("text", "")
        table_text = extracted.get("tableText", "")
        full_text = normalize_pdf_text(f"{text}\n{table_text}")

        if not full_text.strip():
            scan = build_scan_response(filename)
            scan["text"] = ""
            scan["rawText"] = ""
            scan["debug"] = {
                "reason": "empty_pdf_text",
                "textLength": 0,
                "preview": "",
            }
            return scan

        rows = extract_invoice_rows(full_text)
        rows = deduplicate_items(rows)

        if not rows:
            return {
                "ok": False,
                "fileName": filename,
                "error": "Il PDF è stato letto, ma non sono state riconosciute righe articolo utilizzabili.",
                "message": "Il PDF è stato letto, ma non sono state riconosciute righe articolo utilizzabili.",
                "rows": [],
                "matrix": [],
                "text": full_text,
                "rawText": full_text,
                "debug": {
                    "textLength": len(full_text),
                    "preview": full_text[:5000],
                },
            }

        return {
            "ok": True,
            "fileName": filename,
            "rows": rows,
            "matrix": build_matrix(rows),
            "text": full_text,
            "rawText": full_text,
            "debug": {
                "textLength": len(full_text),
                "rowsFound": len(rows),
                "preview": full_text[:1500],
            },
        }

    except Exception as exc:
        message = str(exc) or "Errore interno durante il parsing PDF."

        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "fileName": filename,
                "error": message,
                "message": message,
                "rows": [],
                "matrix": [],
                "text": "",
                "rawText": "",
            },
        )


def extract_text_from_pdf_bytes(file_bytes: bytes) -> Dict[str, str]:
    with tempfile.NamedTemporaryFile(delete=True, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp.flush()

        page_texts = []
        table_texts = []

        with pdfplumber.open(tmp.name) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text(
                    x_tolerance=1,
                    y_tolerance=3,
                    layout=False,
                    keep_blank_chars=False,
                ) or ""

                if page_text.strip():
                    page_texts.append(page_text)

                try:
                    tables = page.extract_tables() or []
                    for table in tables:
                        for row in table:
                            cleaned = [
                                normalize_spaces(cell)
                                for cell in row
                                if normalize_spaces(cell)
                            ]
                            if cleaned:
                                table_texts.append(" ".join(cleaned))
                except Exception:
                    pass

        return {
            "text": "\n".join(page_texts),
            "tableText": "\n".join(table_texts),
        }


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
                row.get("brand", ""),
                row.get("category", ""),
                row.get("position", ""),
            ]
            for row in rows
        ],
    ]


def extract_invoice_rows(text: str) -> List[Dict[str, Any]]:
    normalized = normalize_pdf_text(text)

    bosch_rows = extract_bosch_classic_rows(normalized)
    electronic_rows = extract_electronic_invoice_rows(normalized)
    generic_rows = extract_generic_invoice_rows(normalized)

    candidates = [bosch_rows, electronic_rows, generic_rows]
    candidates = [rows for rows in candidates if rows]

    if not candidates:
        return []

    return max(candidates, key=len)


# ============================================================
# BOSCH CLASSICO
# Formato:
# 0010 8-718-641-615-0 1 56,45 -30,00%(c) -5,00%(d) 37,53 H6
# descrizione nella riga successiva.
# ============================================================

def extract_bosch_classic_rows(text: str) -> List[Dict[str, Any]]:
    lines = [
        normalize_spaces(line)
        for line in str(text or "").split("\n")
        if normalize_spaces(line)
    ]

    results = []

    for index, line in enumerate(lines):
        parsed_line = parse_bosch_classic_product_line(line)

        if not parsed_line:
            continue

        description_parts = []

        same_line_description = parsed_line.get("inlineDescription", "")
        if same_line_description and is_good_description_line(same_line_description):
            description_parts.append(same_line_description)

        for next_index in range(index + 1, min(index + 9, len(lines))):
            next_line = normalize_spaces(lines[next_index])

            if parse_bosch_classic_product_line(next_line):
                break

            if is_document_boundary_line(next_line):
                break

            if is_old_code_line(next_line):
                continue

            if is_transport_or_fee_line(next_line):
                break

            if is_good_description_line(next_line):
                description_parts.append(next_line)
                continue

            if description_parts:
                break

        description = clean_bosch_description(" ".join(description_parts))

        if not description:
            description = parsed_line.get("code", "")

        quantity = safe_number(parsed_line.get("quantity", 0))
        total = safe_number(parsed_line.get("total", 0))
        list_price = safe_number(parsed_line.get("listPrice", 0))

        price = total / quantity if quantity > 0 and total > 0 else list_price

        item = finalize_item(
            {
                "code": parsed_line.get("code", ""),
                "description": description,
                "quantity": quantity,
                "unit": parsed_line.get("unit", "ST"),
                "price": price,
                "total": total,
                "brand": "Bosch",
            }
        )

        if is_valid_material(item):
            results.append(item)

    return results


def parse_bosch_classic_product_line(line: str):
    value = normalize_spaces(line)

    if not value:
        return None

    if is_noise_line(value):
        return None

    value = re.sub(
        r"(\d+[,.]\d+)([-+]\d+[,.]\d+%)",
        r"\1 \2",
        value,
    )

    pattern = re.compile(
        r"^"
        r"(?P<pos>\d{3,5})\s+"
        r"(?P<code>[0-9A-Z](?:[0-9A-Z]*[-./]){1,}[0-9A-Z]+)\s+"
        r"(?P<qty>\d+(?:[.,]\d+)?)\s+"
        r"(?P<list_price>\d+(?:[.,]\d+)?)"
        r"(?P<middle>(?:\s*[-+]?\d+(?:[.,]\d+)?%\([a-z]\)|\s*[-+]?\d+(?:[.,]\d+)?%)*)\s+"
        r"(?P<total>\d+(?:[.,]\d+)?)\s*"
        r"(?P<vat>[A-Z][A-Z0-9]?)?"
        r"(?:\s+(?P<tail>.*))?"
        r"$",
        re.IGNORECASE,
    )

    match = pattern.match(value)

    if not match:
        return None

    return {
        "rowNumber": match.group("pos"),
        "code": match.group("code").strip(),
        "quantity": parse_italian_number(match.group("qty")),
        "listPrice": parse_italian_number(match.group("list_price")),
        "total": parse_italian_number(match.group("total")),
        "unit": "ST",
        "inlineDescription": clean_bosch_description(match.group("tail") or ""),
    }


# ============================================================
# FATTURA ELETTRONICA / ARUBA / ARISTON / BOSCH ELETTRONICA
# Formati:
# 1 GRUPPO RITORNO 1 ST 75,98000000 € 75,98 € 22 % -
# Cod.tipo: COD_FORNITORE, Cod.valore: 65105322
#
# 10 VALVOLA SICUREZZA 1 PCE 44,180 € 44,18 € 22 % -
# Cod.tipo: SAP Material Number, Cod.valore: 87167632110
# ============================================================

def extract_electronic_invoice_rows(text: str) -> List[Dict[str, Any]]:
    section = extract_products_section(text)

    lines = [
        normalize_spaces(line)
        for line in section.split("\n")
        if normalize_spaces(line)
    ]

    results = []
    current_item = None

    for line in lines:
        value = normalize_spaces(line)

        if is_document_boundary_line(value):
            continue

        if is_payment_or_summary_line(value):
            if current_item and is_valid_material(current_item):
                results.append(finalize_item(current_item))
            current_item = None
            break

        code_match = extract_cod_valore(value)

        if code_match and current_item:
            current_item["code"] = code_match
            continue

        product = parse_electronic_product_line(value)

        if product:
            if current_item and is_valid_material(current_item):
                results.append(finalize_item(current_item))

            current_item = product
            continue

        if current_item and is_accessory_or_transport_line(value):
            current_item["skip"] = True
            continue

        if current_item and is_electronic_continuation_line(value):
            current_item["description"] = clean_description(
                f'{current_item.get("description", "")} {value}'
            )

    if current_item and is_valid_material(current_item):
        results.append(finalize_item(current_item))

    cleaned = []

    for item in results:
        if should_skip_electronic_item(item):
            continue

        cleaned.append(item)

    return cleaned


def parse_electronic_product_line(line: str):
    value = normalize_spaces(line)

    if not value:
        return None

    if should_skip_line_as_article(value):
        return None

    value = re.sub(r"\s*€\s*", " € ", value)
    value = normalize_spaces(value)

    patterns = [
        # 1 GRUPPO RITORNO 1 ST 75,98000000 € 75,98 € 22 % -
        r"^(?P<row>\d{1,5})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s*€?\s+(?P<total>\d+(?:[.,]\d+)?)\s*€?\s+(?P<iva>\d{1,2})\s*%",
        # 10 VALVOLA SICUREZZA 1 PCE 44,180 € 44,18 € 22 % -
        r"^(?P<row>\d{1,5})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s*€?\s+(?P<total>\d+(?:[.,]\d+)?)",
        # 50 Addebito trasporto 1 15,000 € 15,00 € 22 % -
        r"^(?P<row>\d{1,5})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<price>\d+(?:[.,]\d+)?)\s*€?\s+(?P<total>\d+(?:[.,]\d+)?)\s*€?\s+(?P<iva>\d{1,2})\s*%",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)

        if not match:
            continue

        row_number = match.groupdict().get("row", "")
        description = clean_description(match.groupdict().get("desc", ""))

        unit = match.groupdict().get("unit", "") or "ST"

        item = {
            "rowNumber": row_number,
            "code": "",
            "description": description,
            "quantity": parse_italian_number(match.groupdict().get("qty", "0")),
            "unit": unit,
            "price": parse_italian_number(match.groupdict().get("price", "0")),
            "total": parse_italian_number(match.groupdict().get("total", "0")),
            "brand": detect_brand_from_text(value),
        }

        if should_skip_electronic_item(item):
            return None

        return item

    return None


def extract_cod_valore(line: str) -> str:
    value = normalize_spaces(line)

    patterns = [
        r"Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)",
        r"Codice\s+fornitore\s*:?\s*([A-Z0-9._/\-]+)",
        r"SAP\s+Material\s+Number\s*,?\s*Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return ""


def should_skip_line_as_article(line: str) -> bool:
    value = normalize_spaces(line)

    if is_transport_or_fee_line(value):
        return True

    if is_payment_or_summary_line(value):
        return True

    if re.match(r"^99\d{2}\s+", value):
        return True

    bad = [
        r"^NR\s+DESCRIZIONE",
        r"^PRODOTTI\s+E\s+SERVIZI",
        r"^Cod\.tipo",
        r"^Tipo dato",
        r"^PILE",
        r"^AEE",
        r"^Riferimento testo",
        r"^Dati ordine",
        r"^Dati DDT",
        r"^DOCUMENTI CORRELATI",
        r"^Tipo doc\.",
        r"^ALLEGATI",
        r"^NOME ALLEGATO",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in bad)


def should_skip_electronic_item(item: Dict[str, Any]) -> bool:
    row_number = str(item.get("rowNumber", "") or "").strip()
    description = str(item.get("description", "") or "").strip()

    if row_number.startswith("99"):
        return True

    if is_bad_description(description):
        return True

    if is_transport_or_fee_line(description):
        return True

    return False


def is_electronic_continuation_line(line: str) -> bool:
    value = normalize_spaces(line)

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


# ============================================================
# GENERIC FALLBACK
# ============================================================

def extract_generic_invoice_rows(text: str) -> List[Dict[str, Any]]:
    lines = [
        normalize_spaces(line)
        for line in str(text or "").split("\n")
        if normalize_spaces(line)
    ]

    results = []

    for index, line in enumerate(lines):
        if is_noise_line(line):
            continue

        parsed = parse_generic_product_line(line)

        if not parsed:
            continue

        description = parsed.get("description", "")

        if not description:
            for next_index in range(index + 1, min(index + 5, len(lines))):
                next_line = lines[next_index]

                if parse_generic_product_line(next_line):
                    break

                if is_good_description_line(next_line):
                    description = next_line
                    break

        item = finalize_item(
            {
                "code": parsed.get("code", ""),
                "description": description,
                "quantity": parsed.get("quantity", 0),
                "unit": parsed.get("unit", "ST"),
                "price": parsed.get("price", 0),
                "total": parsed.get("total", 0),
            }
        )

        if is_valid_material(item):
            results.append(item)

    return results


def parse_generic_product_line(line: str):
    value = normalize_spaces(line)

    patterns = [
        r"^(?P<pos>\d{1,5})\s+(?P<code>[A-Z0-9][A-Z0-9._/\-]{3,})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)",
        r"^(?P<pos>\d{1,5})\s+(?P<code>[A-Z0-9][A-Z0-9._/\-]{3,})\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)

        if not match:
            continue

        return {
            "code": match.groupdict().get("code", ""),
            "description": clean_description(match.groupdict().get("desc", "")),
            "quantity": parse_italian_number(match.groupdict().get("qty", "0")),
            "unit": match.groupdict().get("unit", "ST") or "ST",
            "price": parse_italian_number(match.groupdict().get("price", "0")),
            "total": parse_italian_number(match.groupdict().get("total", "0")),
        }

    return None


# ============================================================
# UTILS
# ============================================================

def normalize_pdf_text(text: str) -> str:
    value = str(text or "")
    value = value.replace("\r", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def extract_products_section(text: str) -> str:
    match = re.search(
        r"PRODOTTI\s+E\s+SERVIZI([\s\S]*?)(METODO\s+DI\s+PAGAMENTO|REGIME\s+FISCALE|DATI\s+AGGIUNTIVI|RIEPILOGO\s+IVA|CALCOLO\s+FATTURA|SCADENZE|TOTALE\s+DOCUMENTO|ALLEGATI)",
        text,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return text


def clean_bosch_description(value: str) -> str:
    text = normalize_spaces(value)

    text = re.sub(r"\bRICAMBIO\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bRICAMBI\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bSOLAR\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bOLD\s+[0-9A-Z./_-]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bold\s+[0-9A-Z./_-]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bD\.d\.T\..*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bVs\. ordine.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bCessione Norm\..*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bAddebito Trasporto.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bContributo Ambientale.*$", "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[-–—,\s]+", "", text)
    text = re.sub(r"[-–—,\s]+$", "", text)

    return text.strip()


def is_old_code_line(line: str) -> bool:
    return bool(
        re.search(
            r"^\s*(old|OLD)\s+[0-9A-Z./_-]+",
            str(line or ""),
            re.IGNORECASE,
        )
    )


def is_transport_or_fee_line(line: str) -> bool:
    return bool(
        re.search(
            r"\b(addebito\s+trasporto|trasporto|trasp|magg\s+trasp|spesa\s+accessoria|contributo\s+ambientale|conai|bollo)\b",
            str(line or ""),
            re.IGNORECASE,
        )
    )


def is_payment_or_summary_line(line: str) -> bool:
    return bool(
        re.search(
            r"\b(METODO\s+DI\s+PAGAMENTO|REGIME\s+FISCALE|DATI\s+AGGIUNTIVI|RIEPILOGO\s+IVA|CALCOLO\s+FATTURA|TOTALE\s+DOCUMENTO|NETTO\s+A\s+PAGARE)\b",
            str(line or ""),
            re.IGNORECASE,
        )
    )


def is_document_boundary_line(line: str) -> bool:
    value = normalize_spaces(line)

    patterns = [
        r"^FATTURA$",
        r"^FATTURA\s+nr\.",
        r"^FORNITORE$",
        r"^CLIENTE$",
        r"^P\.IVA",
        r"^C\.F\.",
        r"^Codice destinatario",
        r"^ROBERT BOSCH",
        r"^Robert Bosch",
        r"^Ariston",
        r"^Capitale ",
        r"^C\.C\.I\.A\.A\.",
        r"^Bollo ",
        r"^BOSCH ",
        r"^Dati da indicare",
        r"^Dest\.",
        r"^Ns\. codice",
        r"^presso ILN",
        r"^Pagina ",
        r"^Cod\.Cliente",
        r"^IVA ",
        r"^Ricevuta ",
        r"^Il documento",
        r"^Per eventuali bonifici",
        r"^UniCredit",
        r"^IBAN",
        r"^SWIFT",
        r"^INDICARE SEMPRE",
        r"^IL RITARDATO",
        r"^Informazioni sulle Sostanze",
        r"^Copia analogica",
        r"^Fattura Nr\.",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def is_noise_line(line: str) -> bool:
    value = normalize_spaces(line)

    if not value:
        return True

    if is_document_boundary_line(value):
        return True

    if is_transport_or_fee_line(value):
        return True

    if is_payment_or_summary_line(value):
        return True

    patterns = [
        r"^RICAMBIO$",
        r"^RICAMBI$",
        r"^SOLAR$",
        r"^Pos\s+Cod\.",
        r"^Descrizione ",
        r"^Cod\.EAN",
        r"^Partita IVA",
        r"^D\.d\.T\.",
        r"^Vs\. ordine",
        r"^del \d",
        r"^Cessione ",
        r"^CL THERMOSERVICE",
        r"^VIA ",
        r"^Via ",
        r"^IT-\d+",
        r"^\d+[,.]\d+$",
        r"^NR\s+DESCRIZIONE",
        r"^PRODOTTI\s+E\s+SERVIZI",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def is_good_description_line(line: str) -> bool:
    value = clean_bosch_description(line)

    if not value:
        return False

    if is_noise_line(value):
        return False

    if parse_bosch_classic_product_line(value):
        return False

    if is_old_code_line(value):
        return False

    if re.search(r"^\d+(?:[.,]\d+)?$", value):
        return False

    if re.search(r"^\d{3,5}\s+", value):
        return False

    return bool(re.search(r"[A-ZÀ-Üa-zà-ü]", value))


def is_accessory_or_transport_line(line: str) -> bool:
    return bool(
        re.search(
            r"\b(addebito\s+trasporto|trasporto|trasp|magg\s+trasp|spesa\s+accessoria|tipo\s+cess\.\s*prestazione)\b",
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
        r"\btrasp\b",
        r"magg\s+trasp",
        r"spesa\s+accessoria",
        r"pagamento",
        r"regime fiscale",
        r"dati aggiuntivi",
        r"riepilogo iva",
        r"calcolo fattura",
        r"ricevuta",
        r"bonifici",
        r"iban",
        r"swift",
        r"totale",
        r"iva vendite",
        r"documenti correlati",
        r"dati ordine",
        r"dati ddt",
        r"allegati",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in bad)


def detect_brand_from_text(value: str) -> str:
    text = str(value or "").lower()

    if "bosch" in text:
        return "Bosch"

    if "ariston" in text:
        return "Ariston"

    return ""


def is_valid_material(item: Dict[str, Any]) -> bool:
    if item.get("skip"):
        return False

    code = str(item.get("code", "") or "").strip()
    description = str(item.get("description", "") or "").strip()

    if not code and not description:
        return False

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
        "brand": str(item.get("brand", "") or "").strip(),
        "category": str(item.get("category", "") or "").strip(),
        "position": str(item.get("position", "") or "").strip(),
    }


def safe_number(value) -> float:
    try:
        number = float(value)
        return number if number == number else 0
    except Exception:
        return 0