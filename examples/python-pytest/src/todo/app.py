from fastapi import FastAPI
from sqlalchemy import select

from .db import Base, Session, engine
from .models import Archive, Todo

app = FastAPI()
Base.metadata.create_all(engine)


@app.get("/todos")
def list_todos():
    with Session() as session:
        return [{"id": t.id, "title": t.title} for t in session.scalars(select(Todo))]


@app.post("/todos")
def create_todo(title: str):
    with Session() as session:
        todo = Todo(title=title)
        session.add(todo)
        session.commit()
        return {"id": todo.id}


@app.post("/todos/{todo_id}/archive")
def archive_todo(todo_id: int):
    with Session() as session:
        session.add(Archive(todo_id=todo_id))
        session.commit()
        return {"archived": todo_id}
