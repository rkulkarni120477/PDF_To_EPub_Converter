from fastapi import FastAPI

app = FastAPI(title="Metadata Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "metadata-service"}


@app.post("/api/v1/metadata")
def manage_metadata() -> dict[str, str]:
    return {"status": "accepted", "service": "metadata-service", "next": "metadata"}
