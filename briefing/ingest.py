from __future__ import annotations

import json
from xml.etree import ElementTree

import feedparser
import httpx

from briefing.providers import MissingCredential, Router
from briefing.store import Store, utc_now
from briefing.textutil import (
    canonical_url,
    chunk_document,
    content_hash,
    doc_id_for,
    heuristic_keep,
    html_to_text,
    lexicon_entities,
)

ATOM = "{http://www.w3.org/2005/Atom}"
USER_AGENT = "senior-brief/0.1"
MAX_BYTES = 1_000_000

ALLOWLIST = [
    {
        "type": "arxiv",
        "name": "arxiv",
        "url": "https://export.arxiv.org/api/query?search_query=cat:cs.DC+OR+cat:cs.DB&sortBy=submittedDate&sortOrder=descending&max_results=5",
    },
    {"type": "rss", "name": "cloudflare", "url": "https://blog.cloudflare.com/rss/"},
    {
        "type": "github",
        "name": "kubernetes",
        "url": "https://api.github.com/repos/kubernetes/kubernetes/releases?per_page=3",
    },
]


def parse_arxiv(xml_text: str) -> list[dict]:
    root = ElementTree.fromstring(xml_text)
    records = []
    for entry in root.findall(ATOM + "entry"):
        title = " ".join((entry.findtext(ATOM + "title") or "").split())
        url = (entry.findtext(ATOM + "id") or "").strip()
        summary = " ".join((entry.findtext(ATOM + "summary") or "").split())
        published = (entry.findtext(ATOM + "published") or "").strip()
        if not title or not url or not summary:
            continue
        records.append(
            {
                "title": title,
                "url": url.replace("/pdf/", "/abs/").replace(".pdf", ""),
                "published_at": published,
                "text": "# Abstract\n\n" + summary,
                "kind": "paper",
                "source_type": "arxiv",
            }
        )
    return records


def parse_rss(payload: str, source_name: str) -> list[dict]:
    parsed = feedparser.parse(payload)
    records = []
    for entry in parsed.entries:
        title = " ".join(str(getattr(entry, "title", "")).split())
        url = str(getattr(entry, "link", "") or "")
        html = ""
        if getattr(entry, "content", None):
            html = entry.content[0].get("value") or ""
        if not html:
            html = str(getattr(entry, "summary", "") or "")
        text = html_to_text(html) if "<" in html else html.strip()
        published = str(getattr(entry, "published", "") or getattr(entry, "updated", "") or "")
        if not title or not url or len(text) < 200:
            continue
        kind = "docs" if "/docs/" in url or "://docs." in url else "blog"
        records.append(
            {
                "title": title,
                "url": url,
                "published_at": published,
                "text": text,
                "kind": kind,
                "source_type": source_name,
            }
        )
    return records


def parse_github_releases(payload: str) -> list[dict]:
    releases = json.loads(payload)
    records = []
    if not isinstance(releases, list):
        return records
    for release in releases:
        title = str(release.get("name") or release.get("tag_name") or "").strip()
        url = str(release.get("html_url") or "").strip()
        body = str(release.get("body") or "").strip()
        if not title or not url or len(body) < 200:
            continue
        records.append(
            {
                "title": title,
                "url": url,
                "published_at": str(release.get("published_at") or ""),
                "text": body,
                "kind": "release",
                "source_type": "github",
            }
        )
    return records


def _gate(router: Router, title: str, text: str) -> tuple[bool, str, list[str]]:
    from briefing.pipeline import GATE_SCHEMA, GATE_SYSTEM

    names = lexicon_entities(title + "\n" + text)
    try:
        result = router.chat("gate", GATE_SYSTEM, "Title: " + title + "\n\n" + text[:6000], GATE_SCHEMA)
    except MissingCredential:
        return heuristic_keep(title, text), "heuristic", names
    for item in result.get("entities") or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip().lower()
            if name and name not in names:
                names.append(name)
    return bool(result.get("keep")), "model", names[:8]


def ingest_records(store: Store, router: Router, records: list[dict]) -> dict:
    report = {"saved": 0, "skipped": 0, "rejected": 0, "errors": []}
    for record in records:
        try:
            url = canonical_url(record["url"])
            text = record["text"].strip()[:100000]
            digest = content_hash(text)
            existing = store.get_document_by_url(url)
            if existing and existing["content_hash"] == digest:
                report["skipped"] += 1
                continue
            keep, gate_mode, entities = _gate(router, record["title"], text)
            doc_id = doc_id_for(url)
            store.upsert_document(
                {
                    "doc_id": doc_id,
                    "source_type": record.get("source_type") or "manual",
                    "canonical_url": url,
                    "title": record["title"].strip(),
                    "published_at": record.get("published_at"),
                    "content_hash": digest,
                    "text": text,
                    "kind": record.get("kind") or "blog",
                    "kept": keep,
                    "gate_mode": gate_mode,
                    "created_at": utc_now(),
                }
            )
            if not keep:
                store.replace_chunks(doc_id, [], None)
                store.replace_entities(doc_id, [])
                report["rejected"] += 1
                continue
            chunks = chunk_document(record["title"], text)
            fingerprint = None
            try:
                index_texts = [chunk["index_text"] for chunk in chunks]
                fingerprint, vectors = router.embed(index_texts, "passage")
                for chunk, vector in zip(chunks, vectors):
                    chunk["embedding"] = vector
            except MissingCredential:
                fingerprint = None
            except Exception as exc:
                report["errors"].append(record["title"] + ": embed: " + str(exc)[:180])
            store.replace_chunks(doc_id, chunks, fingerprint)
            store.replace_entities(doc_id, entities or lexicon_entities(text))
            report["saved"] += 1
        except Exception as exc:
            report["errors"].append(str(exc)[:240])
    return report


def reindex(store: Store, router: Router) -> dict:
    try:
        fingerprint, _sample = router.embed(["passage probe"], "passage")
    except MissingCredential:
        return {"updated": 0, "skipped": "no embed credential"}
    except Exception as exc:
        return {"updated": 0, "error": str(exc)[:240]}
    pending = store.chunks_missing_fingerprint(fingerprint)
    updated = 0
    for start in range(0, len(pending), 16):
        batch = pending[start : start + 16]
        texts = [row["title"] + "\n" + row["section_path"] + "\n" + row["text"] for row in batch]
        _fp, vectors = router.embed(texts, "passage")
        store.save_embeddings(list(zip([row["chunk_id"] for row in batch], vectors)), fingerprint)
        updated += len(batch)
    return {"updated": updated, "fingerprint": fingerprint}


def fetch_allowlist(client: httpx.Client | None = None) -> tuple[list[dict], list[str]]:
    owns_client = client is None
    client = client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}, follow_redirects=True)
    records = []
    errors = []
    try:
        for source in ALLOWLIST:
            try:
                response = client.get(source["url"])
                response.raise_for_status()
                body = response.text[:MAX_BYTES]
                if source["type"] == "arxiv":
                    records.extend(parse_arxiv(body)[:5])
                elif source["type"] == "rss":
                    records.extend(parse_rss(body, source["name"])[:3])
                elif source["type"] == "github":
                    records.extend(parse_github_releases(body)[:2])
            except Exception as exc:
                errors.append(source["name"] + ": " + str(exc)[:180])
    finally:
        if owns_client:
            client.close()
    return records, errors


def load_fixtures() -> list[dict]:
    from pathlib import Path

    path = Path(__file__).resolve().parent / "fixtures" / "articles.json"
    return json.loads(path.read_text(encoding="utf-8"))
