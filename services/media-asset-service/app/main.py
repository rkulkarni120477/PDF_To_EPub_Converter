from fastapi import FastAPI

app = FastAPI(title="Media Asset Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "media-asset-service"}


@app.post("/api/v1/assets")
def manage_asset() -> dict[str, str]:
    return {"status": "accepted", "service": "media-asset-service", "next": "asset"}
