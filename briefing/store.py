from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from cryptography.fernet import Fernet

from briefing.config import Settings, get_settings
from briefing.textutil import cosine, fts_query, pack_vector, rrf, unpack_vector

ROLES = ("gate", "grade", "rewrite", "rerank", "synthesize", "critic")
PROVIDERS = ("xai", "openai", "anthropic", "gemini")

DEFAULT_CONFIG = {
    "embed": {"provider": "", "model": "", "dimensions": 768},
    "roles": {role: {"provider": "", "model": ""} for role in ROLES},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._master_key())
        with self._conn() as conn:
            self._migrate(conn)

    def _master_key(self) -> bytes:
        import os

        env = os.environ.get("BRIEF_MASTER_KEY", "").strip()
        if env:
            return env.encode("utf-8")
        path = self.settings.master_key_path
        if path.exists():
            return path.read_bytes().strip()
        key = Fernet.generate_key()
        path.write_bytes(key)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.settings.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
              doc_id TEXT PRIMARY KEY,
              source_type TEXT NOT NULL,
              canonical_url TEXT NOT NULL UNIQUE,
              title TEXT NOT NULL,
              published_at TEXT,
              content_hash TEXT NOT NULL,
              text TEXT NOT NULL,
              kind TEXT NOT NULL,
              kept INTEGER NOT NULL,
              gate_mode TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
              chunk_id TEXT PRIMARY KEY,
              doc_id TEXT NOT NULL,
              parent_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL,
              section_path TEXT NOT NULL,
              text TEXT NOT NULL,
              parent_text TEXT NOT NULL,
              embedding BLOB,
              embedding_fingerprint TEXT,
              FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
              body,
              chunk_id UNINDEXED
            );
            CREATE TABLE IF NOT EXISTS entities (
              name TEXT PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS entity_links (
              doc_id TEXT NOT NULL,
              name TEXT NOT NULL,
              PRIMARY KEY (doc_id, name)
            );
            CREATE TABLE IF NOT EXISTS editions (
              edition_id TEXT PRIMARY KEY,
              edition_date TEXT NOT NULL,
              body_json TEXT NOT NULL,
              etag TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS credentials (
              provider TEXT PRIMARY KEY,
              ciphertext BLOB NOT NULL,
              last4 TEXT NOT NULL,
              validated_at TEXT,
              test_status TEXT
            );
            CREATE TABLE IF NOT EXISTS role_config (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              config_json TEXT NOT NULL
            );
            """
        )
        row = conn.execute("SELECT config_json FROM role_config WHERE id = 1").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO role_config (id, config_json) VALUES (1, ?)",
                (json.dumps(DEFAULT_CONFIG),),
            )

    def role_config(self) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT config_json FROM role_config WHERE id = 1").fetchone()
        config = json.loads(row["config_json"])
        for role in ROLES:
            config["roles"].setdefault(role, {"provider": "", "model": ""})
        config.setdefault("embed", {"provider": "", "model": "", "dimensions": 768})
        return config

    def save_role_config(self, config: dict) -> dict:
        current = self.role_config()
        embed = config.get("embed") or {}
        current["embed"]["provider"] = str(embed.get("provider") or "")
        current["embed"]["model"] = str(embed.get("model") or "")
        current["embed"]["dimensions"] = 768
        for role in ROLES:
            incoming = (config.get("roles") or {}).get(role) or {}
            current["roles"][role] = {
                "provider": str(incoming.get("provider") or ""),
                "model": str(incoming.get("model") or ""),
            }
        with self._conn() as conn:
            conn.execute(
                "UPDATE role_config SET config_json = ? WHERE id = 1",
                (json.dumps(current),),
            )
        return current

    def credential_views(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT provider, last4, validated_at, test_status FROM credentials ORDER BY provider"
            ).fetchall()
        return [dict(row) for row in rows]

    def save_credential(self, provider: str, api_key: str, status: str) -> dict:
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        now = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO credentials (provider, ciphertext, last4, validated_at, test_status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                  ciphertext = excluded.ciphertext,
                  last4 = excluded.last4,
                  validated_at = excluded.validated_at,
                  test_status = excluded.test_status
                """,
                (provider, self._fernet.encrypt(api_key.encode("utf-8")), api_key[-4:], now, status),
            )
        return {"provider": provider, "last4": api_key[-4:], "validated_at": now, "test_status": status}

    def delete_credential(self, provider: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM credentials WHERE provider = ?", (provider,))

    def api_key(self, provider: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM credentials WHERE provider = ?", (provider,)
            ).fetchone()
        if row is None:
            return None
        return self._fernet.decrypt(row["ciphertext"]).decode("utf-8")

    def get_document_by_url(self, url: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE canonical_url = ?", (url,)
            ).fetchone()
        return dict(row) if row else None

    def get_document(self, doc_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def upsert_document(self, doc: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO documents (
                  doc_id, source_type, canonical_url, title, published_at, content_hash,
                  text, kind, kept, gate_mode, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_id) DO UPDATE SET
                  title = excluded.title,
                  published_at = excluded.published_at,
                  content_hash = excluded.content_hash,
                  text = excluded.text,
                  kind = excluded.kind,
                  kept = excluded.kept,
                  gate_mode = excluded.gate_mode
                """,
                (
                    doc["doc_id"],
                    doc["source_type"],
                    doc["canonical_url"],
                    doc["title"],
                    doc.get("published_at"),
                    doc["content_hash"],
                    doc["text"],
                    doc["kind"],
                    1 if doc["kept"] else 0,
                    doc.get("gate_mode"),
                    doc.get("created_at") or utc_now(),
                ),
            )

    def replace_chunks(self, doc_id: str, chunks: list[dict], fingerprint: str | None) -> None:
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT chunk_id FROM chunks WHERE doc_id = ?", (doc_id,)
            ).fetchall()
            for row in existing:
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (row["chunk_id"],))
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            for chunk in chunks:
                chunk_id = f"{doc_id}_{chunk['ordinal']}"
                embedding = pack_vector(chunk["embedding"]) if chunk.get("embedding") else None
                conn.execute(
                    """
                    INSERT INTO chunks (
                      chunk_id, doc_id, parent_id, ordinal, section_path, text, parent_text,
                      embedding, embedding_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        doc_id,
                        chunk["parent_id"],
                        chunk["ordinal"],
                        chunk["section_path"],
                        chunk["text"],
                        chunk["parent_text"],
                        embedding,
                        fingerprint if embedding else None,
                    ),
                )
                conn.execute(
                    "INSERT INTO chunks_fts (body, chunk_id) VALUES (?, ?)",
                    (chunk["index_text"], chunk_id),
                )

    def replace_entities(self, doc_id: str, names: list[str]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM entity_links WHERE doc_id = ?", (doc_id,))
            for name in names:
                conn.execute("INSERT OR IGNORE INTO entities (name) VALUES (?)", (name,))
                conn.execute(
                    "INSERT OR IGNORE INTO entity_links (doc_id, name) VALUES (?, ?)",
                    (doc_id, name),
                )

    def recent_documents(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT doc_id, title, canonical_url, kind, source_type, published_at, substr(text, 1, 500) AS preview
                FROM documents WHERE kept = 1
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def kept_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE kept = 1").fetchone()
        return int(row["n"])

    def search_fts(self, query: str, limit: int = 40) -> list[str]:
        match = fts_query(query)
        if not match:
            return []
        with self._conn() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT chunk_id FROM chunks_fts
                    WHERE chunks_fts MATCH ?
                    ORDER BY bm25(chunks_fts)
                    LIMIT ?
                    """,
                    (match, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
        return [row["chunk_id"] for row in rows]

    def search_dense(self, vector: list[float], fingerprint: str, limit: int = 40) -> list[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT chunk_id, embedding FROM chunks
                WHERE embedding_fingerprint = ? AND embedding IS NOT NULL
                """,
                (fingerprint,),
            ).fetchall()
        scored = []
        for row in rows:
            score = cosine(vector, unpack_vector(row["embedding"]))
            if score > 0:
                scored.append((score, row["chunk_id"]))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [chunk_id for _, chunk_id in scored[:limit]]

    def parents_for_chunks(self, chunk_ids: list[str]) -> list[dict]:
        if not chunk_ids:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT c.chunk_id, c.doc_id, c.parent_id, c.section_path, c.parent_text,
                       d.title, d.canonical_url, d.kind, d.published_at
                FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                WHERE c.chunk_id IN ({",".join("?" for _ in chunk_ids)})
                """,
                chunk_ids,
            ).fetchall()
        by_id = {row["chunk_id"]: dict(row) for row in rows}
        parents = []
        seen = set()
        for chunk_id in chunk_ids:
            row = by_id.get(chunk_id)
            if not row:
                continue
            key = row["doc_id"] + ":" + row["parent_id"]
            if key in seen:
                continue
            seen.add(key)
            parents.append(
                {
                    "parent_id": key,
                    "doc_id": row["doc_id"],
                    "title": row["title"],
                    "url": row["canonical_url"],
                    "kind": row["kind"],
                    "published_at": row["published_at"],
                    "section_path": row["section_path"],
                    "text": row["parent_text"],
                }
            )
        return parents

    def neighbor_chunk_ids(self, doc_ids: list[str], limit: int = 20) -> list[str]:
        if not doc_ids:
            return []
        placeholders = ",".join("?" for _ in doc_ids)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT other.doc_id, COUNT(*) AS shared
                FROM entity_links mine
                JOIN entity_links other ON other.name = mine.name AND other.doc_id != mine.doc_id
                WHERE mine.doc_id IN ({placeholders})
                GROUP BY other.doc_id
                ORDER BY shared DESC
                LIMIT ?
                """,
                (*doc_ids, limit),
            ).fetchall()
            neighbor_ids = [row["doc_id"] for row in rows]
            if not neighbor_ids:
                return []
            neighbor_marks = ",".join("?" for _ in neighbor_ids)
            chunks = conn.execute(
                f"""
                SELECT chunk_id FROM chunks
                WHERE doc_id IN ({neighbor_marks})
                ORDER BY ordinal ASC
                """,
                neighbor_ids,
            ).fetchall()
        return [row["chunk_id"] for row in chunks]

    def chunks_missing_fingerprint(self, fingerprint: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT c.chunk_id, c.text, d.title, c.section_path
                FROM chunks c
                JOIN documents d ON d.doc_id = c.doc_id
                WHERE c.embedding IS NULL OR c.embedding_fingerprint IS NULL
                   OR c.embedding_fingerprint != ?
                """,
                (fingerprint,),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_embeddings(self, pairs: list[tuple[str, list[float]]], fingerprint: str) -> None:
        with self._conn() as conn:
            for chunk_id, vector in pairs:
                conn.execute(
                    """
                    UPDATE chunks SET embedding = ?, embedding_fingerprint = ?
                    WHERE chunk_id = ?
                    """,
                    (pack_vector(vector), fingerprint, chunk_id),
                )

    def fuse(self, rankings: list[list[str]]) -> list[str]:
        usable = [ranking for ranking in rankings if ranking]
        if not usable:
            return []
        return rrf(usable)

    def save_edition(self, body: dict) -> dict:
        import hashlib

        raw = json.dumps(body, sort_keys=True, ensure_ascii=False)
        etag = '"' + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] + '"'
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO editions (edition_id, edition_date, body_json, etag, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(edition_id) DO UPDATE SET
                  body_json = excluded.body_json,
                  etag = excluded.etag,
                  created_at = excluded.created_at
                """,
                (body["edition_id"], body["edition_date"], raw, etag, utc_now()),
            )
        return {"etag": etag, "body": body}

    def latest_edition(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT body_json, etag FROM editions ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return {"etag": row["etag"], "body": json.loads(row["body_json"])}
