from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
        "service": "pdf-parser",
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

    # TODO: qui va reinserita la tua logica reale del parser PDF.
    # Per ora questa risposta serve solo per verificare che CORS e upload funzionino.

    return {
        "ok": True,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(file_bytes),
        "matrix": [],
    }