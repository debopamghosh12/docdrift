import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

GREETING = os.getenv("GREETING", "unset")

app = FastAPI(title="Items")
_items: dict[int, dict] = {}


class ItemIn(BaseModel):
    name: str
    price: float


@app.get("/greeting")
def greeting():
    return {"greeting": GREETING}


@app.post("/items", status_code=201)
def create_item(item: ItemIn):
    item_id = len(_items) + 1
    _items[item_id] = {
        "id": item_id,
        "name": item.name,
        "price": item.price,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return _items[item_id]


@app.get("/items/{item_id}")
def get_item(item_id: int):
    if item_id not in _items:
        raise HTTPException(status_code=404, detail="Item not found")
    return _items[item_id]
