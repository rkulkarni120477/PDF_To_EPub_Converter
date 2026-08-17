from fastapi import FastAPI

app = FastAPI(title="Validation Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "validation-service"}


@app.post("/api/v1/validations")
def validate_ebook() -> dict[str, str]:
    return {"status": "accepted", "service": "validation-service", "next": "validate"}
