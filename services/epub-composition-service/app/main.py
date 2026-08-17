from fastapi import FastAPI

app = FastAPI(title="ePub Composition Service")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "epub-composition-service"}


@app.post("/api/v1/ebooks/compose")
def compose_ebook() -> dict[str, str]:
    return {"status": "accepted", "service": "epub-composition-service", "next": "compose"}
