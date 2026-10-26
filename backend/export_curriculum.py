"""
Writes the curriculum (programs, tracks, modules, classes) of the database you are looking at into data/curriculum_snapshot.json.gz.

A freshly deployed app has an empty database. seed_curriculum.py loads this snapshot into it, so the deployed app shows the same programs,
tracks, modules and classes as the one the curriculum was edited in, instead of the raw catalog file's structure. Scripts, users and cards are not exported.

Usage (from backend/):  python -m export_curriculum
"""
import datetime
import gzip
import json
import os

from database import SyncSessionLocal
from models import Class, Module, Program, Track

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "curriculum_snapshot.json.gz")
TABLES = (("programs", Program), ("tracks", Track), ("modules", Module), ("classes", Class))


def _plain(value):
    return value.isoformat() if isinstance(value, (datetime.datetime, datetime.date)) else value


def export() -> dict:
    session = SyncSessionLocal()
    try:
        out = {}
        for name, model in TABLES:
            cols = [c.name for c in model.__table__.columns]
            out[name] = [{c: _plain(getattr(row, c)) for c in cols} for row in session.query(model).order_by(model.id).all()]
        return out
    finally:
        session.close()


if __name__ == "__main__":
    data = export()
    with gzip.open(PATH, "wt", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print({k: len(v) for k, v in data.items()}, "->", PATH, f"({os.path.getsize(PATH) // 1024} KB)")
