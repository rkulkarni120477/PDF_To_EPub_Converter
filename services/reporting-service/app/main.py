from fastapi import FastAPI

app = FastAPI(title="Reporting Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "reporting-service"}


@app.post("/api/v1/reports")
def create_report() -> dict[str, str]:
    return {"status": "accepted", "service": "reporting-service", "next": "report"}
