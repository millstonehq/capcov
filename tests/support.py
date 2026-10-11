"""A synthetic project, written to disk, so the adapter is tested against real
files and a real parse rather than against hand-built AST nodes."""

from __future__ import annotations

import tempfile
from pathlib import Path

APP = {
    "models.py": '''
from .db import Base

class Widget(Base):
    __tablename__ = "widgets"

class Crate(Base):
    __tablename__ = "crates"

class Ghost(Base):
    __tablename__ = "ghosts"
''',
    "db.py": "class Base: pass\n",
    "repo.py": '''
from .models import Widget

def fetch_widgets(session):
    return session.execute(select(Widget))

def make_widget(session):
    w = Widget()
    session.add(w)
    return w
''',
    "service.py": '''
from . import repo

def list_widgets(session):
    return repo.fetch_widgets(session)

def create_widget(session):
    return repo.make_widget(session)
''',
    "api.py": '''
from fastapi import APIRouter
from . import service
from .models import Crate

router = APIRouter(prefix="/things")

@router.get("/widgets")
def get_widgets(session):
    return service.list_widgets(session)

@router.post("/widgets")
def post_widget(session):
    return service.create_widget(session)

@router.get("/crates/{crate_id}")
def get_crate(session, crate_id):
    return session.get(Crate, crate_id)
''',
    "orphan.py": '''
from fastapi import APIRouter
from .models import Ghost

lonely = APIRouter(prefix="/lonely")

@lonely.get("/ghosts")
def ghosts(session):
    return Ghost
''',
    "main.py": '''
from fastapi import FastAPI
from . import api

def create_app():
    app = FastAPI()

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    app.include_router(api.router, prefix="/v1")
    return app
''',
    "dynamics.py": '''
def by_literal(obj):
    return getattr(obj, "known_field")

def by_computed(obj, name):
    return getattr(obj, name)

def namespaced(obj):
    return vars(obj)
''',
}


class Project:
    """A throwaway package tree under a temporary directory."""

    def __init__(self, files: dict[str, str] | None = None, package: str = "app") -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.source = self.root / "src"
        pkg = self.source / package
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        for name, body in (files if files is not None else APP).items():
            (pkg / name).write_text(body)

    def write(self, relative: str, body: str) -> None:
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def close(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> Project:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
