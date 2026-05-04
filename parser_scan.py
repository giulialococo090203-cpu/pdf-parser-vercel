def build_scan_response(filename: str) -> dict:
    return {
        "ok": True,
        "mode": "scan",
        "scanDetected": True,
        "fileName": filename,
        "message": "Documento scansito rilevato. Serve compilazione guidata.",
        "rows": [],
        "matrix": [],
    }