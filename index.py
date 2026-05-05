import re
import tempfile
from typing import List, Dict, Any, Optional

import pdfplumber
from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from parser_common import parse_italian_number, clean_description, deduplicate_items
from parser_scan import build_scan_response


app = FastAPI()

ALLOWED_ORIGINS = [
    "https://magazzino-pro.vercel.app",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
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
        "mode": "bruteforce-v3",
        "allowed_origins": ALLOWED_ORIGINS,
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "status": "running",
        "mode": "bruteforce-v3",
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
        extracted = extract_text_from_pdf_bytes(file_bytes)

        full_text = normalize_pdf_text(
            "\n".join(
                [
                    extracted.get("text", ""),
                    extracted.get("tableText", ""),
                    extracted.get("wordText", ""),
                ]
            )
        )

        if not full_text.strip():
            scan = build_scan_response(filename)
            scan["text"] = ""
            scan["rawText"] = ""
            scan["debug"] = {
                "mode": "bruteforce-v3",
                "reason": "empty_pdf_text",
                "textLength": 0,
                "preview": "",
            }
            return scan

        rows = extract_invoice_rows(full_text)
        rows = deduplicate_items(rows)
        rows = final_cleanup_rows(rows)

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
                    "mode": "bruteforce-v3",
                    "textLength": len(full_text),
                    "preview": full_text[:10000],
                    "lines": normalize_pdf_text(full_text).split("\n")[:250],
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
                "mode": "bruteforce-v3",
                "textLength": len(full_text),
                "rowsFound": len(rows),
                "codes": [row.get("code") for row in rows],
                "preview": full_text[:2500],
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


# ============================================================
# ESTRAZIONE TESTO PDF
# ============================================================

def extract_text_from_pdf_bytes(file_bytes: bytes) -> Dict[str, str]:
    with tempfile.NamedTemporaryFile(delete=True, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp.flush()

        page_texts = []
        table_texts = []
        word_texts = []

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
                    layout_text = page.extract_text(
                        x_tolerance=1,
                        y_tolerance=3,
                        layout=True,
                        keep_blank_chars=False,
                    ) or ""

                    if layout_text.strip():
                        page_texts.append(layout_text)
                except Exception:
                    pass

                try:
                    words = page.extract_words(
                        x_tolerance=1,
                        y_tolerance=3,
                        keep_blank_chars=False,
                        use_text_flow=True,
                    ) or []

                    if words:
                        word_lines = rebuild_lines_from_words(words)
                        if word_lines:
                            word_texts.append("\n".join(word_lines))
                except Exception:
                    pass

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
            "wordText": "\n".join(word_texts),
        }


def rebuild_lines_from_words(words: List[Dict[str, Any]]) -> List[str]:
    if not words:
        return []

    sorted_words = sorted(
        words,
        key=lambda w: (
            round(float(w.get("top", 0)) / 3) * 3,
            float(w.get("x0", 0)),
        ),
    )

    lines = []
    current_top = None
    current_words = []

    for word in sorted_words:
        top = float(word.get("top", 0))
        text = str(word.get("text", "") or "").strip()

        if not text:
            continue

        if current_top is None:
            current_top = top
            current_words = [word]
            continue

        if abs(top - current_top) <= 4:
            current_words.append(word)
            current_top = (current_top + top) / 2
        else:
            line = join_words_as_line(current_words)
            if normalize_spaces(line):
                lines.append(line)

            current_top = top
            current_words = [word]

    if current_words:
        line = join_words_as_line(current_words)
        if normalize_spaces(line):
            lines.append(line)

    return lines


def join_words_as_line(words: List[Dict[str, Any]]) -> str:
    sorted_words = sorted(words, key=lambda w: float(w.get("x0", 0)))
    return normalize_spaces(" ".join(str(w.get("text", "") or "") for w in sorted_words))


# ============================================================
# MATRIX RISPOSTA
# ============================================================

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


# ============================================================
# PARSER PRINCIPALE
# ============================================================

def extract_invoice_rows(text: str) -> List[Dict[str, Any]]:
    normalized = normalize_pdf_text(text)

    lines = [
        normalize_spaces(line)
        for line in normalized.split("\n")
        if normalize_spaces(line)
    ]

    product_lines = extract_product_section_lines(lines)

    candidates = []

    candidates.extend(parse_electronic_invoice_lines(product_lines))
    candidates.extend(parse_bosch_classic_invoice_lines(lines))
    candidates.extend(parse_bosch_dense_invoice_lines(lines))
    candidates.extend(parse_generic_structured_lines(product_lines))

    candidates = [finalize_item(item) for item in candidates]
    candidates = [
        item
        for item in candidates
        if is_valid_material(item) and not should_skip_item(item)
    ]

    return merge_and_deduplicate_by_best_key(candidates)


def extract_product_section_lines(lines: List[str]) -> List[str]:
    output = []
    inside = False

    for line in lines:
        value = normalize_spaces(line)

        if re.search(r"PRODOTTI\s+E\s+SERVIZI", value, re.IGNORECASE):
            inside = True
            continue

        if inside and re.search(
            r"^(METODO\s+DI\s+PAGAMENTO|REGIME\s+FISCALE|DATI\s+AGGIUNTIVI|RIEPILOGO\s+IVA|CALCOLO\s+FATTURA|SCADENZE|TOTALE\s+DOCUMENTO|ALLEGATI|DOCUMENTI\s+CORRELATI)\b",
            value,
            re.IGNORECASE,
        ):
            break

        if inside:
            output.append(value)

    return output if output else lines


# ============================================================
# FATTURE ELETTRONICHE / ARUBA / ARISTON / BOSCH ELETTRONICO
# ============================================================

def parse_electronic_invoice_lines(lines: List[str]) -> List[Dict[str, Any]]:
    results = []
    current_item = None

    for line in lines:
        value = normalize_spaces(line)

        if not value:
            continue

        if is_document_or_payment_boundary(value):
            if current_item and is_valid_material(current_item) and not should_skip_item(current_item):
                results.append(current_item)
            current_item = None
            continue

        code = extract_cod_valore(value)

        if code and current_item:
            current_item["code"] = code
            continue

        product = parse_electronic_product_line(value)

        if product:
            if current_item and is_valid_material(current_item) and not should_skip_item(current_item):
                results.append(current_item)

            current_item = product

            inline_code = extract_cod_valore(value)
            if inline_code:
                current_item["code"] = inline_code

            continue

        if current_item and is_likely_code_only_line(value):
            loose_code = extract_code_from_loose_line(value)
            if loose_code:
                current_item["code"] = loose_code
            continue

        if current_item and is_product_description_continuation(value):
            current_item["description"] = clean_joined_description(
                f'{current_item.get("description", "")} {value}'
            )

    if current_item and is_valid_material(current_item) and not should_skip_item(current_item):
        results.append(current_item)

    return results


def parse_electronic_product_line(line: str) -> Optional[Dict[str, Any]]:
    value = normalize_spaces(line)

    if not value:
        return None

    if should_skip_line(value):
        return None

    value = value.replace("€", " € ")
    value = normalize_spaces(value)

    patterns = [
        r"^(?P<row>\d{1,5})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s*€?\s+(?P<total>\d+(?:[.,]\d+)?)\s*€?\s+(?P<iva>\d{1,2})\s*%",
        r"^(?P<row>\d{1,5})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)\s+(?P<iva>\d{1,2})\s*%",
        r"^(?P<row>\d{1,5})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<price>\d+(?:[.,]\d+)?)\s*€?\s+(?P<total>\d+(?:[.,]\d+)?)\s*€?\s+(?P<iva>\d{1,2})\s*%",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)

        if not match:
            continue

        row_number = match.groupdict().get("row", "")
        description = clean_joined_description(match.groupdict().get("desc", ""))
        quantity = parse_italian_number(match.groupdict().get("qty", "0"))
        unit = match.groupdict().get("unit", "") or "ST"
        price = parse_italian_number(match.groupdict().get("price", "0"))
        total = parse_italian_number(match.groupdict().get("total", "0"))

        item = {
            "rowNumber": row_number,
            "code": "",
            "description": description,
            "quantity": quantity,
            "unit": unit,
            "price": price,
            "total": total,
            "brand": detect_brand_from_text(value),
            "category": "",
            "position": "",
        }

        if should_skip_item(item):
            return None

        return item

    return None


def extract_cod_valore(line: str) -> str:
    value = normalize_spaces(line)

    patterns = [
        r"Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)",
        r"Codice\s+fornitore\s*:?\s*([A-Z0-9._/\-]+)",
        r"SAP\s+Material\s+Number\s*,?\s*Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)",
        r"COD_FORNITORE\s*,?\s*Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)",
        r"Cod\.?\s*tipo\s*:?\s*[^,]+,\s*Cod\.?\s*valore\s*:?\s*([A-Z0-9._/\-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            return match.group(1).strip()

    return ""


# ============================================================
# BOSCH CLASSICO
# ============================================================

def parse_bosch_classic_invoice_lines(lines: List[str]) -> List[Dict[str, Any]]:
    results = []

    for index, line in enumerate(lines):
        parsed = parse_bosch_classic_line(line)

        if not parsed:
            continue

        description = parsed.get("description", "")

        if not description:
            description = find_next_description(lines, index)

        description = clean_joined_description(description)

        if not description:
            continue

        if normalize_key(description) == normalize_key(parsed.get("code", "")):
            continue

        quantity = safe_number(parsed.get("quantity", 0))
        total = safe_number(parsed.get("total", 0))
        list_price = safe_number(parsed.get("listPrice", 0))
        price = total / quantity if quantity > 0 and total > 0 else list_price

        item = {
            "rowNumber": parsed.get("rowNumber", ""),
            "code": parsed.get("code", ""),
            "description": description,
            "quantity": quantity,
            "unit": parsed.get("unit", "ST"),
            "price": price,
            "total": total,
            "brand": "Bosch",
            "category": "",
            "position": "",
        }

        if is_valid_material(item) and not should_skip_item(item):
            results.append(item)

    return results


def parse_bosch_classic_line(line: str) -> Optional[Dict[str, Any]]:
    value = normalize_spaces(line)

    if not value:
        return None

    if should_skip_line(value):
        return None

    pattern = re.compile(
        r"^"
        r"(?P<row>\d{3,5})\s+"
        r"(?P<code>[0-9A-Z][0-9A-Z\-./]{4,})\s+"
        r"(?P<qty>\d+(?:[.,]\d+)?)\s+"
        r"(?P<list_price>\d+(?:[.,]\d+)?)"
        r"(?P<discounts>(?:\s*[-+]?\d+(?:[.,]\d+)?%\(?[a-z]?\)?|\s*[-+]?\d+(?:[.,]\d+)?%)*)\s+"
        r"(?P<total>\d+(?:[.,]\d+)?)"
        r"(?:\s+(?P<tail>.*))?"
        r"$",
        re.IGNORECASE,
    )

    match = pattern.match(value)

    if not match:
        return None

    tail = normalize_spaces(match.group("tail") or "")
    tail = re.sub(r"^[A-Z]\d?\s*", "", tail).strip()

    return {
        "rowNumber": match.group("row"),
        "code": match.group("code"),
        "quantity": parse_italian_number(match.group("qty")),
        "listPrice": parse_italian_number(match.group("list_price")),
        "total": parse_italian_number(match.group("total")),
        "unit": "ST",
        "description": clean_joined_description(tail),
    }


def find_next_description(lines: List[str], index: int) -> str:
    parts = []

    for next_index in range(index + 1, min(index + 8, len(lines))):
        value = normalize_spaces(lines[next_index])

        if not value:
            continue

        if parse_bosch_classic_line(value):
            break

        if parse_electronic_product_line(value):
            break

        if is_document_or_payment_boundary(value):
            break

        if is_likely_code_only_line(value):
            continue

        if should_skip_line(value):
            continue

        if is_good_description_line(value):
            parts.append(value)

            if len(parts) >= 1:
                break

    return clean_joined_description(" ".join(parts))


# ============================================================
# BOSCH DENSO / LINEE SPEZZATE
# ============================================================

def parse_bosch_dense_invoice_lines(lines: List[str]) -> List[Dict[str, Any]]:
    results = []

    for index, line in enumerate(lines):
        value = normalize_spaces(line)

        if should_skip_line(value):
            continue

        match = re.match(
            r"^(?P<row>\d{3,5})\s+(?P<code>[0-9A-Z][0-9A-Z\-./]{4,})(?:\s+(?P<rest>.*))?$",
            value,
            re.IGNORECASE,
        )

        if not match:
            continue

        code = match.group("code")
        row_number = match.group("row")
        window = " ".join(lines[index:index + 8])
        window = normalize_spaces(window)

        quantity, list_price, total = extract_numbers_near_bosch_code(window, code)

        if quantity <= 0:
            continue

        description = find_next_description(lines, index)

        if not description:
            continue

        if normalize_key(description) == normalize_key(code):
            continue

        price = total / quantity if quantity > 0 and total > 0 else list_price

        item = {
            "rowNumber": row_number,
            "code": code,
            "description": description,
            "quantity": quantity,
            "unit": "ST",
            "price": price,
            "total": total,
            "brand": "Bosch",
            "category": "",
            "position": "",
        }

        if is_valid_material(item) and not should_skip_item(item):
            results.append(item)

    return results


def extract_numbers_near_bosch_code(text: str, code: str):
    value = normalize_spaces(text)

    code_pos = value.find(code)
    if code_pos >= 0:
        value = value[code_pos + len(code):]

    tokens = re.findall(
        r"[-+]?\d+(?:[.,]\d+)?%?\(?[a-z]?\)?|\d+(?:[.,]\d+)?",
        value,
    )

    numeric_values = []

    for token in tokens:
        if "%" in token:
            continue

        number = parse_italian_number(token)

        if number > 0:
            numeric_values.append(number)

    if len(numeric_values) >= 3:
        quantity = numeric_values[0]
        list_price = numeric_values[1]
        total = numeric_values[-1]
        return quantity, list_price, total

    if len(numeric_values) >= 2:
        quantity = numeric_values[0]
        list_price = numeric_values[1]
        total = numeric_values[-1]
        return quantity, list_price, total

    return 0, 0, 0


# ============================================================
# GENERICO STRUTTURATO SOLO NELLA SEZIONE PRODOTTI
# ============================================================

def parse_generic_structured_lines(lines: List[str]) -> List[Dict[str, Any]]:
    results = []

    for index, line in enumerate(lines):
        value = normalize_spaces(line)

        if should_skip_line(value):
            continue

        parsed = parse_generic_product_line(value)

        if not parsed:
            continue

        if not parsed.get("description"):
            parsed["description"] = find_next_description(lines, index)

        if is_valid_material(parsed) and not should_skip_item(parsed):
            results.append(parsed)

    return results


def parse_generic_product_line(line: str) -> Optional[Dict[str, Any]]:
    value = normalize_spaces(line)

    patterns = [
        r"^(?P<row>\d{1,5})\s+(?P<code>[A-Z0-9][A-Z0-9._/\-]{4,})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)",
        r"^(?P<row>\d{1,5})\s+(?P<code>[A-Z0-9][A-Z0-9._/\-]{4,})\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)

        if not match:
            continue

        return {
            "rowNumber": match.groupdict().get("row", ""),
            "code": match.groupdict().get("code", ""),
            "description": clean_joined_description(match.groupdict().get("desc", "")),
            "quantity": parse_italian_number(match.groupdict().get("qty", "0")),
            "unit": match.groupdict().get("unit", "ST") or "ST",
            "price": parse_italian_number(match.groupdict().get("price", "0")),
            "total": parse_italian_number(match.groupdict().get("total", "0")),
            "brand": detect_brand_from_text(value),
            "category": "",
            "position": "",
        }

    return None


# ============================================================
# DEDUPLICA E PULIZIA FINALE
# ============================================================

def merge_and_deduplicate_by_best_key(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key = {}

    for item in items:
        code = normalize_key(item.get("code", ""))
        description = normalize_key(item.get("description", ""))
        quantity = round(safe_number(item.get("quantity", 0)), 4)

        if not code and not description:
            continue

        if code:
            key = f"code:{code}:qty:{quantity}"
        else:
            key = f"desc:{description}:qty:{quantity}"

        existing = by_key.get(key)

        if not existing:
            by_key[key] = item
            continue

        by_key[key] = choose_better_item(existing, item)

    sorted_items = list(by_key.values())

    def sort_key(item):
        row = str(item.get("rowNumber", "") or "")
        try:
            return int(re.sub(r"\D", "", row) or "999999")
        except Exception:
            return 999999

    return sorted(sorted_items, key=sort_key)


def final_cleanup_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cleaned = []

    for row in rows:
        item = finalize_item(row)

        if not is_valid_material(item):
            continue

        if should_skip_item(item):
            continue

        code_key = normalize_key(item.get("code", ""))
        desc_key = normalize_key(item.get("description", ""))

        if code_key and desc_key == code_key:
            continue

        if is_bad_description(item.get("description", "")):
            continue

        cleaned.append(item)

    return merge_and_deduplicate_by_best_key(cleaned)


def choose_better_item(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    return b if item_quality_score(b) > item_quality_score(a) else a


def item_quality_score(item: Dict[str, Any]) -> int:
    score = 0

    if item.get("code"):
        score += 30

    description = str(item.get("description", "") or "")

    if description:
        score += min(40, len(description) // 2)

    if item.get("quantity"):
        score += 10

    if item.get("price"):
        score += 10

    if item.get("brand"):
        score += 5

    return score


# ============================================================
# UTILS
# ============================================================

def normalize_pdf_text(text: str) -> str:
    value = str(text or "")
    value = value.replace("\r", "")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def clean_joined_description(value: str) -> str:
    text = normalize_spaces(value)
    text = clean_description(text)

    remove_patterns = [
        r"\bRICAMBIO\b",
        r"\bRICAMBI\b",
        r"\bSOLAR\b",
        r"\bOLD\s+[0-9A-Z./_-]+",
        r"\bold\s+[0-9A-Z./_-]+",
        r"\bD\.d\.T\..*$",
        r"\bVs\. ordine.*$",
        r"\bCessione Norm\..*$",
        r"\bAddebito Trasporto.*$",
        r"\bContributo Ambientale.*$",
        r"\bTipo dato:.*$",
        r"\bRiferimento testo:.*$",
        r"\bCod\.tipo:.*$",
        r"\bCod\.valore:.*$",
        r"\bCodice fornitore:.*$",
        r"\bC\.C\.I\.A\.A\..*$",
        r"\bIscritta Tribun.*$",
    ]

    for pattern in remove_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[-–—,\s]+", "", text)
    text = re.sub(r"[-–—,\s]+$", "", text)

    return text.strip()


def should_skip_line(line: str) -> bool:
    value = normalize_spaces(line)

    if not value:
        return True

    if is_transport_or_fee_line(value):
        return True

    if is_document_or_payment_boundary(value):
        return True

    patterns = [
        r"^NR\s+DESCRIZIONE",
        r"^PRODOTTI\s+E\s+SERVIZI",
        r"^Cod\.tipo",
        r"^Tipo dato",
        r"^Riferimento testo",
        r"^PILE\b",
        r"^AEE\b",
        r"^Dati ordine",
        r"^Dati DDT",
        r"^DOCUMENTI CORRELATI",
        r"^Tipo doc\.",
        r"^ALLEGATI",
        r"^NOME ALLEGATO",
        r"^FORNITORE$",
        r"^CLIENTE$",
        r"^P\.IVA",
        r"^C\.F\.",
        r"^Codice destinatario",
        r"^Copia analogica",
        r"^Fattura Nr\.",
        r"^Pagina ",
        r"^Pag\.",
        r"^IVA ",
        r"^RF01",
        r"^UNICREDIT",
        r"^IBAN",
        r"^BIC",
        r"^SWIFT",
        r"^CAUSALE DOCUMENTO",
        r"^Descrizione causale",
        r"^DATI TRASPORTO",
        r"^INFORMAZIONI RESA",
        r"^ROBERT\s+BOSCH",
        r"^BOSCH\s+S\.?P\.?A",
        r"^ARISTON\s+",
        r"^CAPITALE\s+SOCIALE",
        r"^REGISTRO\s+IMPRESE",
        r"^SEDE\s+LEGALE",
        r"^PARTITA\s+IVA",
        r"^CODICE\s+FISCALE",
        r"^C\.?C\.?I\.?A\.?A",
        r"^R\.?E\.?A\.?",
        r"^ISCRITTA\s+TRIBUN",
        r"^TRIBUNALE",
        r"^CAMERA\s+DI\s+COMMERCIO",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)


def is_document_or_payment_boundary(line: str) -> bool:
    value = normalize_spaces(line)

    return bool(
        re.search(
            r"\b(METODO\s+DI\s+PAGAMENTO|REGIME\s+FISCALE|DATI\s+AGGIUNTIVI|RIEPILOGO\s+IVA|CALCOLO\s+FATTURA|SCADENZE|TOTALE\s+DOCUMENTO|NETTO\s+A\s+PAGARE|DOCUMENTI\s+CORRELATI|ALLEGATI)\b",
            value,
            re.IGNORECASE,
        )
    )


def is_transport_or_fee_line(line: str) -> bool:
    return bool(
        re.search(
            r"\b(addebito\s+trasporto|trasporto|trasp|magg\s+trasp|spesa\s+accessoria|tipo\s+cess\.\s*prestazione|contributo\s+ambientale|conai|bollo)\b",
            str(line or ""),
            re.IGNORECASE,
        )
    )


def should_skip_item(item: Dict[str, Any]) -> bool:
    row_number = str(item.get("rowNumber", "") or "").strip()
    description = str(item.get("description", "") or "").strip()
    code = str(item.get("code", "") or "").strip()

    if row_number.startswith("99"):
        return True

    if normalize_key(description) == normalize_key(code):
        return True

    if is_transport_or_fee_line(description):
        return True

    if is_bad_description(description):
        return True

    return False


def is_bad_description(description: str) -> bool:
    value = normalize_spaces(description)

    if not value:
        return True

    if re.match(r"^[0-9A-Z][0-9A-Z._/\-]{4,}$", value, re.IGNORECASE):
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
        r"causale documento",
        r"informazioni resa",
        r"copia analogica",
        r"fattura generata",
        r"robert\s+bosch",
        r"bosch\s+s\.?p\.?a",
        r"società",
        r"societa",
        r"capitale\s+sociale",
        r"partita\s+iva",
        r"codice\s+fiscale",
        r"registro\s+imprese",
        r"sede\s+legale",
        r"condizioni\s+di\s+pagamento",
        r"fattura\s+nr",
        r"fattura\s+n",
        r"c\.?c\.?i\.?a\.?a",
        r"iscritta\s+tribun",
        r"tribunale",
        r"camera\s+di\s+commercio",
        r"rea\s+[0-9]",
        r"r\.?e\.?a\.?",
        r"n\.\s*reg",
        r"pec\s*:",
        r"www\.",
        r"http",
        r"@",
        r"tel\.?",
        r"fax",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in bad)


def is_likely_code_only_line(line: str) -> bool:
    value = normalize_spaces(line)

    if extract_cod_valore(value):
        return True

    if re.match(r"^[A-Z0-9][A-Z0-9._/\-]{4,}$", value, re.IGNORECASE):
        return True

    return False


def extract_code_from_loose_line(line: str) -> str:
    explicit = extract_cod_valore(line)

    if explicit:
        return explicit

    match = re.search(r"\b([A-Z0-9][A-Z0-9._/\-]{4,})\b", line, re.IGNORECASE)

    return match.group(1).strip() if match else ""


def is_product_description_continuation(line: str) -> bool:
    value = normalize_spaces(line)

    if not value:
        return False

    if should_skip_line(value):
        return False

    if parse_electronic_product_line(value):
        return False

    if parse_bosch_classic_line(value):
        return False

    if is_likely_code_only_line(value):
        return False

    if re.match(r"^\d{1,5}\s+", value):
        return False

    return bool(re.search(r"[A-ZÀ-Üa-zà-ü]", value))


def is_good_description_line(line: str) -> bool:
    value = clean_joined_description(line)

    if not value:
        return False

    if should_skip_line(value):
        return False

    if is_likely_code_only_line(value):
        return False

    if re.match(r"^[0-9A-Z][0-9A-Z._/\-]{4,}$", value, re.IGNORECASE):
        return False

    if re.match(r"^\d+(?:[.,]\d+)?$", value):
        return False

    if re.match(r"^\d{1,5}\s+", value):
        return False

    bad_description_lines = [
        r"ROBERT\s+BOSCH",
        r"BOSCH\s+S\.?P\.?A",
        r"SOCIETÀ",
        r"SOCIETA",
        r"CAPITALE\s+SOCIALE",
        r"PARTITA\s+IVA",
        r"CODICE\s+FISCALE",
        r"VIA\s+",
        r"PIAZZA\s+",
        r"SEDE\s+LEGALE",
        r"REGISTRO\s+IMPRESE",
        r"FATTURA",
        r"CLIENTE",
        r"FORNITORE",
        r"DESTINATARIO",
        r"CONDIZIONI\s+DI\s+PAGAMENTO",
        r"C\.?C\.?I\.?A\.?A",
        r"ISCRITTA\s+TRIBUN",
        r"TRIBUNALE",
        r"CAMERA\s+DI\s+COMMERCIO",
    ]

    if any(re.search(pattern, value, re.IGNORECASE) for pattern in bad_description_lines):
        return False

    if len(value) < 4:
        return False

    return bool(re.search(r"[A-ZÀ-Üa-zà-ü]", value))


def detect_brand_from_text(value: str) -> str:
    text = str(value or "").lower()

    if "bosch" in text:
        return "Bosch"

    if "ariston" in text:
        return "Ariston"

    return ""


def is_valid_material(item: Dict[str, Any]) -> bool:
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
        "description": clean_joined_description(item.get("description", "")),
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