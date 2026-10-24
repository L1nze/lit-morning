"""SQLite 去重存储：已推送过的论文不再重复出现。"""
from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .paper import Paper

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id         TEXT PRIMARY KEY,
    dedup_key  TEXT,
    title      TEXT,
    source     TEXT,
    first_seen TEXT
);
CREATE INDEX IF NOT EXISTS idx_seen_key ON seen(dedup_key);
"""


class SeenStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # timeout=30s：Windows 上计划任务重叠运行时避免 "database is locked" 立刻崩
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")   # 读写不互斥，更稳
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def known_keys(self) -> set[str]:
        rows = self.conn.execute("SELECT dedup_key FROM seen").fetchall()
        return {r[0] for r in rows if r[0]}

    def filter_new(self, papers: list[Paper]) -> list[Paper]:
        known = self.known_keys()
        seen_ids: set[str] = set()
        out = []
        for p in papers:
            if p.dedup_key in known or p.dedup_key in seen_ids:
                continue
            seen_ids.add(p.dedup_key)
            out.append(p)
        return out

    def mark_seen(self, papers: list[Paper], when: date | None = None) -> None:
        when = when or date.today()
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen (id, dedup_key, title, source, first_seen) VALUES (?,?,?,?,?)",
            [(p.id, p.dedup_key, p.title[:300], p.source, when.isoformat()) for p in papers],
        )
        self.conn.commit()

    def stats_by_day(self, days: int = 14) -> list[tuple[str, str, int]]:
        return self.conn.execute(
            "SELECT first_seen, source, COUNT(*) FROM seen "
            "WHERE first_seen >= date('now', ?) "
            "GROUP BY first_seen, source ORDER BY first_seen DESC",
            (f"-{days} day",),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
