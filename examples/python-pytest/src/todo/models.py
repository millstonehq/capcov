from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class Todo(Base):
    __tablename__ = "todos"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    done: Mapped[bool] = mapped_column(default=False)


class Archive(Base):
    __tablename__ = "archived_todos"
    id: Mapped[int] = mapped_column(primary_key=True)
    todo_id: Mapped[int] = mapped_column(ForeignKey("todos.id"))
