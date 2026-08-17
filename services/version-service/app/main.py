from fastapi import FastAPI

app = FastAPI(title="Version Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "version-service"}


@app.post("/api/v1/versions")
def create_version() -> dict[str, str]:
    return {"status": "accepted", "service": "version-service", "next": "version"}
