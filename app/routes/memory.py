import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import begin, get_db, row, rows

router = APIRouter(prefix="/api/memory", tags=["memory"])
Kind = Literal['like', 'dislike', 'maybe', 'condition', 'habit', 'buying_pattern', 'recipe_note', 'cost_note']


class NoteIn(BaseModel):
    kind: Kind
    content: str = Field(min_length=1, max_length=20000)
    source: str = Field(default="chat", min_length=1, max_length=200)


class NoteEdit(BaseModel):
    content: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    kind: Optional[Kind] = None
    active: Optional[bool] = None


@router.get("")
def list_notes(kind: Optional[Kind] = None, q: Optional[str] = None, active: Optional[bool] = True,
               db=Depends(get_db), user=Depends(current_user)):
    clauses, args = [], []
    if kind is not None:
        clauses.append("n.kind=?")
        args.append(kind)
    if active is not None:
        clauses.append("n.active=?")
        args.append(int(active))
    if q and q.strip():
        clauses.append("n.id IN (SELECT rowid FROM household_notes_fts WHERE household_notes_fts MATCH ?)")
        args.append(q)
    sql = "SELECT n.* FROM household_notes n"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    try:
        return rows(db.execute(sql + " ORDER BY n.updated_at DESC, n.id DESC", args))
    except sqlite3.OperationalError:
        raise HTTPException(400, "Invalid search query")


@router.post("", status_code=201)
def add_note(data: NoteIn, db=Depends(get_db), user=Depends(current_user)):
    if not data.content.strip():
        raise HTTPException(400, "Content cannot be empty")
    begin(db)
    cur = db.execute("INSERT INTO household_notes(kind,content,source) VALUES(?,?,?)",
                     (data.kind, data.content.strip(), data.source))
    db.commit()
    return row(db.execute("SELECT * FROM household_notes WHERE id=?", (cur.lastrowid,)))


@router.put("/{note_id}")
def edit_note(note_id: int, data: NoteEdit, db=Depends(get_db), user=Depends(current_user)):
    updates = data.model_dump(exclude_unset=True)
    if any(value is None for value in updates.values()):
        raise HTTPException(400, "Fields cannot be null")
    if 'content' in updates:
        updates['content'] = updates['content'].strip()
        if not updates['content']:
            raise HTTPException(400, "Content cannot be empty")
    begin(db)
    if not db.execute("SELECT 1 FROM household_notes WHERE id=?", (note_id,)).fetchone():
        db.rollback()
        raise HTTPException(404, "Note not found")
    assignments = [f"{key}=?" for key in updates]
    assignments.append("updated_at=datetime('now')")
    db.execute("UPDATE household_notes SET " + ','.join(assignments) + " WHERE id=?",
               [*updates.values(), note_id])
    db.commit()
    return row(db.execute("SELECT * FROM household_notes WHERE id=?", (note_id,)))


@router.delete("/{note_id}")
def delete_note(note_id: int, db=Depends(get_db), user=Depends(current_user)):
    begin(db)
    cur = db.execute("DELETE FROM household_notes WHERE id=?", (note_id,))
    if not cur.rowcount:
        db.rollback()
        raise HTTPException(404, "Note not found")
    db.commit()
    return {"ok": True}
