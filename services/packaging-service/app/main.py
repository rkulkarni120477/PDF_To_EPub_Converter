from fastapi import FastAPI

app = FastAPI(title="Packaging Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "packaging-service"}


@app.post("/api/v1/packages")
def create_package() -> dict[str, str]:
    return {"status": "accepted", "service": "packaging-service", "next": "package"}
