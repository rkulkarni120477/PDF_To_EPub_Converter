from fastapi import FastAPI

app = FastAPI(title="Download Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "download-service"}


@app.post("/api/v1/downloads")
def create_download() -> dict[str, str]:
    return {"status": "accepted", "service": "download-service", "next": "download"}
