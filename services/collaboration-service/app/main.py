from fastapi import FastAPI

app = FastAPI(title="Collaboration Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "collaboration-service"}


@app.post("/api/v1/collaboration/reviews")
def create_review() -> dict[str, str]:
    return {"status": "accepted", "service": "collaboration-service", "next": "review"}
