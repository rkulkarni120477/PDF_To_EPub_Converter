from fastapi import FastAPI

app = FastAPI(title="Preview Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "preview-service"}


@app.post("/api/v1/previews")
def create_preview() -> dict[str, str]:
    return {"status": "accepted", "service": "preview-service", "next": "preview"}
