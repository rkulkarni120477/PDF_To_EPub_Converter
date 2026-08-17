from fastapi import FastAPI

app = FastAPI(title="Content Processing Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "content-processing-service"}


@app.post("/api/v1/documents/process")
def process_document() -> dict[str, str]:
    return {"status": "accepted", "service": "content-processing-service", "next": "process"}
