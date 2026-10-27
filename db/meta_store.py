"""
============================================================
[FILE] db/meta_store.py
[역할] SQLite 메타데이터 저장 + FTS5 전문검색
[테이블] documents (원문 저장), documents_fts (전문검색 인덱스)
[합칠 때] OCR 원본 이미지 경로 컬럼 추가, 카메라 세션 ID 컬럼 추가
============================================================
"""

import sqlite3
from pathlib import Path


class MetaStore:
    def __init__(self, path: str = "./data/documents.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id      TEXT UNIQUE NOT NULL,
                title       TEXT NOT NULL,
                content     TEXT NOT NULL,
                char_count  INTEGER NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                doc_id,
                title,
                content,
                content=documents,
                content_rowid=id,
                tokenize='porter unicode61'
            );

            CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
                INSERT INTO documents_fts(rowid, doc_id, title, content)
                VALUES (new.id, new.doc_id, new.title, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
                INSERT INTO documents_fts(documents_fts, rowid, doc_id, title, content)
                VALUES ('delete', old.id, old.doc_id, old.title, old.content);
            END;
        """)
        self._conn.commit()

    def save(self, doc_id: str, title: str, content: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO documents (doc_id, title, content, char_count) VALUES (?, ?, ?, ?)",
            (doc_id, title, content, len(content)),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_recent(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT doc_id, title, char_count, created_at FROM documents ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_by_id(self, doc_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT doc_id, title, content, char_count, created_at FROM documents WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()
        return dict(row) if row else None

    def search(self, query: str, limit: int = 5) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT d.doc_id, d.title, d.content, d.char_count, d.created_at,
                   rank
            FROM documents_fts fts
            JOIN documents d ON d.id = fts.rowid
            WHERE documents_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [
            {
                "doc_id": r["doc_id"],
                "title": r["title"],
                "content": r["content"],
                "char_count": r["char_count"],
                "created_at": r["created_at"],
                "score": round(-r["rank"], 4),
            }
            for r in rows
        ]

    def get_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) as cnt FROM documents").fetchone()
        return row["cnt"]

    def get_stats(self) -> dict:
        row = self._conn.execute(
            "SELECT COUNT(*) as total, COALESCE(AVG(char_count), 0) as avg_chars FROM documents"
        ).fetchone()
        return {"total": row["total"], "avg_chars": round(row["avg_chars"])}

    def reset(self):
        self._conn.executescript("""
            DELETE FROM documents;
            INSERT INTO documents_fts(documents_fts) VALUES ('delete-all');
        """)
        self._conn.commit()

    def close(self):
        self._conn.close()
