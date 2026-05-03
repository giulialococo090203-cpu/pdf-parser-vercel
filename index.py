from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import re
import io
from typing import List, Dict, Any

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PRODUCT_SECTION_RE = re.compile(
    r"PRODOTTI E SERVIZI(.*?)(METODO DI PAGAMENTO|REGIME FISCALE|DATI AGGIUNTIVI|RIEPILOGO IVA|CALCOLO FATTURA)",
    re.IGNORECASE | re.DOTALL,
)

PRODUCT_LINE_RE = re.compile(
    r"^(\d+)\s+(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Z]{1,5})\s+(\d+(?:[.,]\d+)?)\s+€\s+(\d+(?:[.,]\d+)?)\s+€",
    re.IGNORECASE,
)

CODE_RE = re.compile(r"Cod\.valore:\s*([A-Z0-9\-]+)", re.IGNORECASE)
HEADER_RE = re.compile(r"^NR\s+DESCRIZIONE\s+QUANTITA", re.IGNORECASE)
END_RE = re.compile(
    r"METODO DI PAGAMENTO|REGIME FISCALE|DATI AGGIUNTIVI|RIEPILOGO IVA|CALCOLO FATTURA",
    re.IGNORECASE,
)

def parse_italian_number(value: str) -> float:
    cleaned = str(value or "").replace(".", "").replace(",", ".")
    cleaned = re.sub(r"[^\d\.\-]", "", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0

def clean_description(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[-–—\s]+", "", text)
    text = re.sub(r"\bPILE,\s*Riferimento testo:.*$", "", text, flags=re.IGNORECASE).strip()
    return text

def extract_text_with_pdfplumber(file_bytes: bytes) -> str:
    pages_text = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages_text.append(text)
    return "\n".join(pages_text)

def finalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "code": str(item.get("code", "")).strip(),
        "description": clean_description(item.get("description", "")),
        "quantity": item.get("quantity", 0) or 0,
        "unit": str(item.get("unit", "ST")).strip(),
        "price": item.get("price", 0) or 0,
        "total": item.get("total", 0) or 0,
    }

def extract_invoice_rows(text: str) -> List[Dict[str, Any]]:
    normalized = text.replace("\r", "")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{2,}", "\n", normalized).strip()

    section_match = PRODUCT_SECTION_RE.search(normalized)
    if not section_match:
        return []

    section = section_match.group(1)
    lines = [line.strip() for line in section.split("\n") if line.strip()]

    results = []
    current_item = None

    for line in lines:
        if HEADER_RE.search(line):
            continue

        if END_RE.search(line):
            if current_item:
                results.append(finalize_item(current_item))
                current_item = None
            break

        code_match = CODE_RE.search(line)
        if code_match and current_item:
            current_item["code"] = code_match.group(1).strip()
            continue

        product_match = PRODUCT_LINE_RE.match(line)
        if product_match:
            if current_item:
                results.append(finalize_item(current_item))

            current_item = {
                "rowNumber": product_match.group(1),
                "description": clean_description(product_match.group(2)),
                "quantity": parse_italian_number(product_match.group(3)),
                "unit": product_match.group(4).strip(),
                "price": parse_italian_number(product_match.group(5)),
                "total": parse_italian_number(product_match.group(6)),
                "code": "",
            }
            continue

        if current_item and len(line) > 2 and not CODE_RE.search(line):
            current_item["description"] = clean_description(
                f'{current_item["description"]} {line}'
            )

    if current_item:
        results.append(finalize_item(current_item))

    return [item for item in results if item["description"] and item["quantity"] > 0]

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
        raise HTTPException(status_code=400, detail="PDF senza testo estraibile.")

    rows = extract_invoice_rows(text)

    if not rows:
        raise HTTPException(status_code=400, detail="Nessuna riga articolo riconosciuta nel PDF.")

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
                    row.get("unit", "ST"),
                    row.get("price", 0),
                    "",
                    "",
                    "",
                ]
                for row in rows
            ],
        ],
    }