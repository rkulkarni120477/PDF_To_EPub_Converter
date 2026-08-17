from fastapi import FastAPI

app = FastAPI(title="Content Enrichment Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "content-enrichment-service"}


@app.post("/api/v1/documents/enrich")
def enrich_document() -> dict[str, str]:
    return {"status": "accepted", "service": "content-enrichment-service", "next": "enrich"}
