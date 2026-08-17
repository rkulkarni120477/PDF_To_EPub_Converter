from fastapi import FastAPI

app = FastAPI(title="Job Orchestration Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "job-orchestration-service"}


@app.post("/api/v1/jobs")
def create_job() -> dict[str, str]:
    return {"status": "accepted", "service": "job-orchestration-service", "next": "orchestrate"}
