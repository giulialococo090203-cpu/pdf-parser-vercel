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
        "mode": "traceability-parser-v7",
        "allowed_origins": ALLOWED_ORIGINS,
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "status": "running",
        "mode": "traceability-parser-v7",
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
                    extracted.get("layoutText", ""),
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
                "mode": "traceability-parser-v7",
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
                    "mode": "traceability-parser-v7",
                    "textLength": len(full_text),
                    "preview": full_text[:15000],
                    "lines": full_text.split("\n")[:600],
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
                "mode": "traceability-parser-v7",
                "textLength": len(full_text),
                "rowsFound": len(rows),
                "codes": [row.get("code") for row in rows],
                "preview": full_text[:5000],
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
        layout_texts = []
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
                        layout_texts.append(layout_text)
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
                        rebuilt = rebuild_lines_from_words(words)
                        if rebuilt:
                            word_texts.append("\n".join(rebuilt))
                except Exception:
                    pass

                try:
                    tables = page.extract_tables() or []
                    for table in tables:
                        for row in table:
                            cells = [
                                normalize_spaces(cell)
                                for cell in row
                                if normalize_spaces(cell)
                            ]
                            if cells:
                                table_texts.append(" ".join(cells))
                except Exception:
                    pass

        return {
            "text": "\n".join(page_texts),
            "layoutText": "\n".join(layout_texts),
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
# RISPOSTA MATRIX
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

    product_lines = extract_products_section(lines)

    candidates = []

    # Sequenza: prima parser più affidabile, poi fallback.
    candidates.extend(parse_bosch_invoice_rows(lines))
    candidates.extend(parse_electronic_invoice_rows(product_lines))
    candidates.extend(parse_generic_structured_rows(product_lines))

    # Selezione: mantieni solo voci vere.
    candidates = [finalize_item(item) for item in candidates]
    candidates = [
        item
        for item in candidates
        if is_valid_material(item) and not should_skip_item(item)
    ]

    # Deduplica finale.
    return merge_and_deduplicate(candidates)


def extract_products_section(lines: List[str]) -> List[str]:
    output = []
    inside = False

    for line in lines:
        value = normalize_spaces(line)

        if re.search(r"PRODOTTI\s+E\s+SERVIZI", value, re.IGNORECASE):
            inside = True
            continue

        if inside and re.search(
            r"^(METODO\s+DI\s+PAGAMENTO|REGIME\s+FISCALE|DATI\s+AGGIUNTIVI|RIEPILOGO\s+IVA|CALCOLO\s+FATTURA|SCADENZE|TOTALE\s+DOCUMENTO|ALLEGATI|DOCUMENTI\s+CORRELATI|DATI\s+TRASPORTO)\b",
            value,
            re.IGNORECASE,
        ):
            break

        if inside:
            output.append(value)

    return output if len(output) >= 2 else lines


# ============================================================
# PARSER BOSCH
# ============================================================

def parse_bosch_invoice_rows(lines: List[str]) -> List[Dict[str, Any]]:
    results = []

    for index, line in enumerate(lines):
        value = normalize_spaces(line)

        if is_hard_document_line(value):
            continue

        parsed = parse_bosch_commercial_line(value)

        if not parsed:
            continue

        code = parsed["code"]
        description = clean_joined_description(parsed.get("description", ""))

        if not is_good_material_description(description):
            description = find_bosch_description(lines, index, code)

        description = clean_joined_description(description)

        if not is_good_material_description(description):
            continue

        if normalize_key(description) == normalize_key(code):
            continue

        quantity = safe_number(parsed.get("quantity", 0))
        total = safe_number(parsed.get("total", 0))
        list_price = safe_number(parsed.get("listPrice", 0))
        price = total / quantity if quantity > 0 and total > 0 else list_price

        item = {
            "rowNumber": parsed.get("rowNumber", ""),
            "code": code,
            "description": description,
            "quantity": quantity,
            "unit": parsed.get("unit", "ST") or "ST",
            "price": price,
            "total": total,
            "brand": "Bosch",
            "category": "",
            "position": "",
        }

        if is_valid_material(item) and not should_skip_item(item):
            results.append(item)

    return results


def parse_bosch_commercial_line(line: str) -> Optional[Dict[str, Any]]:
    value = normalize_spaces(line)

    if not value:
        return None

    if is_hard_document_line(value):
        return None

    code_pattern = r"(?P<code>[0-9]-[0-9]{3}-[0-9]{3}-[0-9]{3}(?:-[0-9])?)"

    patterns = [
        # 0010 8-738-724-555 1 12,30 -30,00%(c) -5,00%(d) 8,18
        rf"^(?P<row>\d{{3,5}})\s+{code_pattern}\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<list_price>\d+(?:[.,]\d+)?)(?P<middle>(?:\s+[-+]?\d+(?:[.,]\d+)?%\(?[a-z]?\)?|\s+[-+]?\d+(?:[.,]\d+)?%|\s+[A-Z]{{1,4}})*)\s+(?P<total>\d+(?:[.,]\d+)?)(?:\s+(?P<tail>.*))?$",

        # 0010 8-738-724-555 1 ST 12,30 8,18
        rf"^(?P<row>\d{{3,5}})\s+{code_pattern}\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{{1,8}})\s+(?P<list_price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)(?:\s+(?P<tail>.*))?$",

        # 0010 8-738-724-555 CAVO DEL SENSORE 1 ST 12,30 8,18
        rf"^(?P<row>\d{{3,5}})\s+{code_pattern}\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{{1,8}})\s+(?P<list_price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)$",

        # 8-738-724-555 CAVO DEL SENSORE 1 ST 12,30 8,18
        rf"^{code_pattern}\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{{1,8}})\s+(?P<list_price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)$",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)

        if not match:
            continue

        groups = match.groupdict()
        code = groups.get("code", "")

        if not looks_like_bosch_code(code):
            continue

        row_number = groups.get("row", "")
        tail = groups.get("tail", "") or ""
        desc = groups.get("desc", "") or ""

        possible_description = clean_joined_description(desc or tail)

        return {
            "rowNumber": row_number,
            "code": code,
            "description": possible_description,
            "quantity": parse_italian_number(groups.get("qty", "0")),
            "unit": groups.get("unit", "ST") or "ST",
            "listPrice": parse_italian_number(groups.get("list_price", "0")),
            "total": parse_italian_number(groups.get("total", "0")),
        }

    return None


def find_bosch_description(lines: List[str], index: int, code: str) -> str:
    # Descrizione sotto la riga commerciale.
    for offset in range(1, 8):
        pos = index + offset

        if pos >= len(lines):
            break

        value = normalize_spaces(lines[pos])

        if not value:
            continue

        if parse_bosch_commercial_line(value):
            break

        if contains_bosch_code(value):
            continue

        if is_hard_document_line(value):
            continue

        if is_discount_or_price_line(value):
            continue

        if is_transport_or_fee_line(value):
            continue

        cleaned = clean_joined_description(value)

        if is_good_material_description(cleaned):
            return cleaned

    # Descrizione sopra la riga commerciale.
    for offset in range(1, 5):
        pos = index - offset

        if pos < 0:
            break

        value = normalize_spaces(lines[pos])

        if not value:
            continue

        if parse_bosch_commercial_line(value):
            break

        if contains_bosch_code(value):
            continue

        if is_hard_document_line(value):
            continue

        if is_discount_or_price_line(value):
            continue

        if is_transport_or_fee_line(value):
            continue

        cleaned = clean_joined_description(value)

        if is_good_material_description(cleaned):
            return cleaned

    current = normalize_spaces(lines[index])
    return extract_description_from_same_line(current, code)


# ============================================================
# PARSER FATTURA ELETTRONICA
# ============================================================

def parse_electronic_invoice_rows(lines: List[str]) -> List[Dict[str, Any]]:
    results = []
    current_item = None

    for line in lines:
        value = normalize_spaces(line)

        if not value:
            continue

        if is_document_boundary(value):
            if current_item and is_valid_material(current_item) and not should_skip_item(current_item):
                results.append(current_item)
            current_item = None
            continue

        code = extract_cod_valore(value)

        if code and current_item and looks_like_product_code(code):
            current_item["code"] = code
            continue

        product = parse_electronic_product_line(value)

        if product:
            if current_item and is_valid_material(current_item) and not should_skip_item(current_item):
                results.append(current_item)

            current_item = product
            continue

        if current_item and is_good_material_description(value):
            current_item["description"] = clean_joined_description(
                f'{current_item.get("description", "")} {value}'
            )

    if current_item and is_valid_material(current_item) and not should_skip_item(current_item):
        results.append(current_item)

    return results


def parse_electronic_product_line(line: str) -> Optional[Dict[str, Any]]:
    value = normalize_spaces(line)

    if not value or should_skip_line(value):
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

        groups = match.groupdict()
        description = clean_joined_description(groups.get("desc", ""))

        if not is_good_material_description(description):
            return None

        item = {
            "rowNumber": groups.get("row", ""),
            "code": "",
            "description": description,
            "quantity": parse_italian_number(groups.get("qty", "0")),
            "unit": groups.get("unit", "ST") or "ST",
            "price": parse_italian_number(groups.get("price", "0")),
            "total": parse_italian_number(groups.get("total", "0")),
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
            code = match.group(1).strip()
            return code if looks_like_product_code(code) else ""

    return ""


# ============================================================
# PARSER GENERICO
# ============================================================

def parse_generic_structured_rows(lines: List[str]) -> List[Dict[str, Any]]:
    results = []

    for line in lines:
        value = normalize_spaces(line)

        if should_skip_line(value):
            continue

        parsed = parse_generic_product_line(value)

        if parsed and is_valid_material(parsed) and not should_skip_item(parsed):
            results.append(parsed)

    return results


def parse_generic_product_line(line: str) -> Optional[Dict[str, Any]]:
    value = normalize_spaces(line)

    patterns = [
        r"^(?P<row>\d{1,5})\s+(?P<code>[A-Z0-9][A-Z0-9._/\-]{4,})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)",
        r"^(?P<code>[A-Z0-9][A-Z0-9._/\-]{4,})\s+(?P<desc>.+?)\s+(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Z]{1,8})\s+(?P<price>\d+(?:[.,]\d+)?)\s+(?P<total>\d+(?:[.,]\d+)?)",
    ]

    for pattern in patterns:
        match = re.match(pattern, value, re.IGNORECASE)

        if not match:
            continue

        groups = match.groupdict()
        code = groups.get("code", "")

        if not looks_like_product_code(code):
            continue

        description = clean_joined_description(groups.get("desc", ""))

        if not is_good_material_description(description):
            continue

        return {
            "rowNumber": groups.get("row", ""),
            "code": code,
            "description": description,
            "quantity": parse_italian_number(groups.get("qty", "0")),
            "unit": groups.get("unit", "ST") or "ST",
            "price": parse_italian_number(groups.get("price", "0")),
            "total": parse_italian_number(groups.get("total", "0")),
            "brand": detect_brand_from_text(value),
            "category": "",
            "position": "",
        }

    return None


# ============================================================
# FILTRI
# ============================================================

def should_skip_line(line: str) -> bool:
    value = normalize_spaces(line)

    if not value:
        return True

    if is_hard_document_line(value):
        return True

    if is_transport_or_fee_line(value):
        return True

    if is_discount_or_price_line(value):
        return True

    return False


def is_hard_document_line(line: str) -> bool:
    value = normalize_spaces(line)

    hard_patterns = [
        r"^NR\s+DESCRIZIONE",
        r"^PRODOTTI\s+E\s+SERVIZI$",
        r"^Cod\.tipo",
        r"^Tipo dato",
        r"^Riferimento testo",
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
        r"^IT-\d{5}\b",
        r"^PALERMO\b",
        r"^ROMA\b",
        r"^MILANO\b",
        r"^NAPOLI\b",
        r"^TORINO\b",
        r"^VIA\b",
        r"^PIAZZA\b",
        r"^Totale\b",
        r"^Imponibile\b",
        r"^Bollo\b",
        r"^Bollo\s+assolto",
        r"^Marca\s+da\s+bollo",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in hard_patterns)


def is_document_boundary(line: str) -> bool:
    value = normalize_spaces(line)

    return bool(
        re.search(
            r"\b(METODO\s+DI\s+PAGAMENTO|REGIME\s+FISCALE|DATI\s+AGGIUNTIVI|RIEPILOGO\s+IVA|CALCOLO\s+FATTURA|SCADENZE|TOTALE\s+DOCUMENTO|NETTO\s+A\s+PAGARE|DOCUMENTI\s+CORRELATI|ALLEGATI|DATI\s+TRASPORTO)\b",
            value,
            re.IGNORECASE,
        )
    )


def is_transport_or_fee_line(line: str) -> bool:
    return bool(
        re.search(
            r"\b(addebito\s+trasporto|trasporto|trasp|magg\s+trasp|spesa\s+accessoria|tipo\s+cess\.\s*prestazione|contributo\s+ambientale|conai|bollo|marca\s+da\s+bollo)\b",
            str(line or ""),
            re.IGNORECASE,
        )
    )


def is_discount_or_price_line(line: str) -> bool:
    value = normalize_spaces(line)

    if not value:
        return True

    if re.match(r"^[\d\s.,%()+\-]+$", value):
        return True

    if re.search(r"%\s*\([a-z]\)", value, re.IGNORECASE):
        cleaned = re.sub(r"%\s*\([a-z]\)", "", value, flags=re.IGNORECASE)
        cleaned = re.sub(r"[-+\d\s.,%()]+", "", cleaned).strip()

        if not cleaned:
            return True

    return False


def should_skip_item(item: Dict[str, Any]) -> bool:
    row_number = str(item.get("rowNumber", "") or "").strip()
    description = str(item.get("description", "") or "").strip()
    code = str(item.get("code", "") or "").strip()

    if row_number.startswith("99"):
        return True

    if normalize_key(description) == normalize_key(code):
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

    if is_discount_or_price_line(value):
        return True

    bad = [
        r"\bIT-\d{5}\b",
        r"\bPALERMO\s+PA\b",
        r"\bROMA\b",
        r"\bMILANO\b",
        r"\bNAPOLI\b",
        r"\bTORINO\b",
        r"\bC\.?C\.?I\.?A\.?A\b",
        r"\bISCRITTA\s+TRIBUN\b",
        r"\bTRIBUNALE\b",
        r"\bPARTITA\s+IVA\b",
        r"\bCODICE\s+FISCALE\b",
        r"\bREGISTRO\s+IMPRESE\b",
        r"\bCAPITALE\s+SOCIALE\b",
        r"\bSEDE\s+LEGALE\b",
        r"\bROBERT\s+BOSCH\s+S\.?P\.?A\b",
        r"\bBOSCH\s+S\.?P\.?A\b",
        r"\bSOCIETÀ\b",
        r"\bSOCIETA\b",
        r"addebito\s+trasporto",
        r"spesa\s+accessoria",
        r"metodo\s+di\s+pagamento",
        r"riepilogo\s+iva",
        r"calcolo\s+fattura",
        r"totale\s+documento",
        r"netto\s+a\s+pagare",
        r"\biban\b",
        r"\bswift\b",
        r"www\.",
        r"http",
        r"@",
        r"\btel\.?\b",
        r"\bfax\b",
        r"\bbollo\b",
        r"\bbollo\s+assolto\b",
        r"\bmarca\s+da\s+bollo\b",
    ]

    return any(re.search(pattern, value, re.IGNORECASE) for pattern in bad)


def is_good_material_description(description: str) -> bool:
    value = clean_joined_description(description)

    if not value:
        return False

    if len(value) < 3:
        return False

    if is_bad_description(value):
        return False

    if is_discount_or_price_line(value):
        return False

    if re.match(r"^[0-9A-Z][0-9A-Z._/\-]{4,}$", value, re.IGNORECASE):
        return False

    words = re.findall(r"[A-ZÀ-Üa-zà-ü]{3,}", value)

    if len(words) >= 1 and len(value) >= 5:
        return True

    return False


# ============================================================
# PULIZIA DESCRIZIONI
# ============================================================

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
        r"\bC\.?C\.?I\.?A\.?A\..*$",
        r"\bIscritta Tribun.*$",
        r"\bPALERMO\s+PA\b",
        r"\bIT-\d{5}\b",
        r"\bBollo\s+assolto.*$",
        r"\bMarca\s+da\s+bollo.*$",
        r"%\s*\([a-z]\)",
    ]

    for pattern in remove_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"^[-–—,\s]+", "", text)
    text = re.sub(r"[-–—,\s]+$", "", text)

    return text.strip()


def extract_description_from_same_line(line: str, code: str) -> str:
    value = normalize_spaces(line)

    if not code or code not in value:
        return ""

    before = normalize_spaces(value.split(code, 1)[0])
    after = normalize_spaces(value.split(code, 1)[1])

    after_clean = after
    after_clean = re.sub(r"\b\d+(?:[.,]\d+)?\b", " ", after_clean)
    after_clean = re.sub(r"[-+]?\d+(?:[.,]\d+)?%\(?[a-z]?\)?", " ", after_clean)
    after_clean = clean_joined_description(after_clean)

    if is_good_material_description(after_clean):
        return after_clean

    before_clean = re.sub(r"^\d{1,5}\s*", "", before)
    before_clean = clean_joined_description(before_clean)

    if is_good_material_description(before_clean):
        return before_clean

    return ""


# ============================================================
# CODICI
# ============================================================

def contains_bosch_code(line: str) -> bool:
    return bool(
        re.search(
            r"\b[0-9]-[0-9]{3}-[0-9]{3}-[0-9]{3}(?:-[0-9])?\b",
            str(line or ""),
            re.IGNORECASE,
        )
    )


def looks_like_bosch_code(code: str) -> bool:
    return bool(
        re.match(
            r"^[0-9]-[0-9]{3}-[0-9]{3}-[0-9]{3}(?:-[0-9])?$",
            str(code or ""),
            re.IGNORECASE,
        )
    )


def looks_like_product_code(code: str) -> bool:
    value = str(code or "").strip()

    if not value:
        return False

    if looks_like_bosch_code(value):
        return True

    if re.match(r"^IT-\d{5}$", value, re.IGNORECASE):
        return False

    if re.match(r"^[A-Z]{1,3}-\d{3,5}$", value, re.IGNORECASE):
        return False

    if re.match(r"^[A-Z0-9][A-Z0-9._/\-]{4,}$", value, re.IGNORECASE):
        return True

    return False


# ============================================================
# FINALIZZAZIONE / DEDUPLICA
# ============================================================

def finalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rowNumber": str(item.get("rowNumber", "") or "").strip(),
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


def is_valid_material(item: Dict[str, Any]) -> bool:
    code = str(item.get("code", "") or "").strip()
    description = str(item.get("description", "") or "").strip()

    if not code and not description:
        return False

    if not is_good_material_description(description):
        return False

    quantity = safe_number(item.get("quantity", 0))
    price = safe_number(item.get("price", 0))

    return bool(description) and quantity > 0 and price >= 0


def merge_and_deduplicate(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_key = {}

    for item in items:
        code_key = normalize_key(item.get("code", ""))
        desc_key = normalize_key(item.get("description", ""))

        if code_key:
            key = f"code:{code_key}"
        else:
            key = f"desc:{desc_key}"

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

        if normalize_key(item.get("description", "")) == normalize_key(item.get("code", "")):
            continue

        cleaned.append(item)

    return merge_and_deduplicate(cleaned)


def choose_better_item(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    return b if item_quality_score(b) > item_quality_score(a) else a


def item_quality_score(item: Dict[str, Any]) -> int:
    score = 0

    if item.get("code"):
        score += 50

    description = str(item.get("description", "") or "")

    if description:
        score += min(80, len(description))

    if item.get("quantity"):
        score += 10

    if item.get("price"):
        score += 10

    if item.get("total"):
        score += 5

    if item.get("brand"):
        score += 5

    if is_bad_description(description):
        score -= 200

    if normalize_key(description) == normalize_key(item.get("code", "")):
        score -= 200

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


def detect_brand_from_text(value: str) -> str:
    text = str(value or "").lower()

    if "bosch" in text:
        return "Bosch"

    if "ariston" in text:
        return "Ariston"

    return ""


def safe_number(value) -> float:
    try:
        number = float(value)
        return number if number == number else 0
    except Exception:
        return 0