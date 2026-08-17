from fastapi import FastAPI

app = FastAPI(title="PDF Extraction Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "pdf-extraction-service"}


@app.post("/api/v1/extractions")
def create_extraction() -> dict[str, str]:
    return {"status": "accepted", "service": "pdf-extraction-service", "next": "extract"}
