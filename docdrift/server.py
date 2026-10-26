"""FastAPI webhook server that receives GitHub events."""

from fastapi import FastAPI

app = FastAPI(title="DocDrift")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
