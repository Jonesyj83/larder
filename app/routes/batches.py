from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import get_db, row, rows

router = APIRouter(prefix="/api/cooked-batches", tags=["cooked batches"])


def _public(b):
    out = dict(b)
    out["is_freezer"] = bool(out.pop("is_freezer"))
    return out


def _get(db, batch_id):
    b = row(db.execute("""
        SELECT c.*, l.name AS location, l.is_freezer
        FROM cooked_batches c JOIN locations l ON l.id = c.location_id
        WHERE c.id = ?""", (batch_id,)))
    if not b:
        raise HTTPException(404, "That stored meal isn't there anymore")
    return _public(b)


@router.get("")
def list_batches(db=Depends(get_db), user=Depends(current_user)):
    """Cooked meals sitting in the fridge or freezer, waiting to be eaten."""
    out = rows(db.execute("""
        SELECT c.*, l.name AS location, l.is_freezer
        FROM cooked_batches c JOIN locations l ON l.id = c.location_id
        WHERE c.portions_left > 0 ORDER BY c.cooked_at, c.id"""))
    return [_public(b) for b in out]


class UseIn(BaseModel):
    amount: float = Field(default=1, gt=0, le=1000)


@router.put("/{batch_id}/use")
def use_batch(batch_id: int, data: UseIn, db=Depends(get_db), user=Depends(current_user)):
    """Take a portion out of a stored batch. Empties and clears it once nothing's left."""
    b = row(db.execute("SELECT * FROM cooked_batches WHERE id = ?", (batch_id,)))
    if not b:
        raise HTTPException(404, "That stored meal isn't there anymore")
    left = round(b["portions_left"] - data.amount, 6)
    if left <= 0:
        db.execute("DELETE FROM cooked_batches WHERE id = ?", (batch_id,))
        db.commit()
        return {"used_up": True}
    db.execute("UPDATE cooked_batches SET portions_left = ? WHERE id = ?", (left, batch_id))
    db.commit()
    return _get(db, batch_id)


@router.delete("/{batch_id}")
def remove_batch(batch_id: int, db=Depends(get_db), user=Depends(current_user)):
    """Thrown out, eaten outside the app, whatever - just take it off the list."""
    db.execute("DELETE FROM cooked_batches WHERE id = ?", (batch_id,))
    db.commit()
    return {"ok": True}
