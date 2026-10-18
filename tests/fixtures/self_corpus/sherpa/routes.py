from fastapi import APIRouter, FastAPI

app = FastAPI()
router = APIRouter(prefix="/v1")


@router.get("/ping")
def ping():
    return {"ok": True}


@app.post("/items")
def add_item():
    return {}
