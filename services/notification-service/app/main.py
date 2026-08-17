from fastapi import FastAPI

app = FastAPI(title="Notification Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "notification-service"}


@app.post("/api/v1/notifications")
def send_notification() -> dict[str, str]:
    return {"status": "accepted", "service": "notification-service", "next": "notify"}
