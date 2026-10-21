from __future__ import annotations

from briefing.config import Settings
from briefing.ingest import ingest_records, load_fixtures, parse_arxiv, parse_github_releases
from briefing.pipeline import attach_sources, run_edition
from briefing.providers import Router
from briefing.store import Store
from briefing.textutil import canonical_url, chunk_document, rrf


def test_canonical_url_strips_tracking():
    assert (
        canonical_url("https://Example.com/posts/a/?utm_source=x&id=1#frag")
        == "https://example.com/posts/a?id=1"
    )


def test_chunker_keeps_sections():
    chunks = chunk_document("Title", "# One\n\nAlpha.\n\n# Two\n\nBeta.")
    assert [chunk["section_path"] for chunk in chunks] == ["One", "Two"]
    assert "Alpha" in chunks[0]["parent_text"]


def test_rrf_prefers_items_in_both_lists():
    fused = rrf([["a", "b"], ["b", "c"]])
    assert fused[0] == "b"


def test_attach_sources_uses_catalog_url_only(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_DATA_DIR", str(tmp_path))
    store = Store(Settings())
    store.upsert_document(
        {
            "doc_id": "d_test",
            "source_type": "fixture",
            "canonical_url": "https://example.com/post",
            "title": "Post",
            "published_at": "2026-01-01",
            "content_hash": "abc",
            "text": "body",
            "kind": "blog",
            "kept": True,
            "gate_mode": "test",
        }
    )
    brief = attach_sources(
        {
            "items": [
                {
                    "title": "Item",
                    "decision": "d",
                    "mechanism": "m",
                    "what_breaks": "w",
                    "what_to_copy": "c",
                    "citations": ["d_test", "missing"],
                    "sources": [{"url": "https://evil.example/phish"}],
                }
            ],
            "dropped": [],
        },
        store,
    )
    assert brief["items"][0]["sources"] == [
        {
            "source_id": "d_test",
            "kind": "blog",
            "title": "Post",
            "url": "https://example.com/post",
            "heading": None,
            "published_at": "2026-01-01",
        }
    ]


def test_parsers_extract_links():
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>https://arxiv.org/abs/2401.09670</id>
        <title>DistServe</title>
        <published>2024-01-18T00:00:00Z</published>
        <summary>Prefill and decode are split across pools.</summary>
      </entry>
    </feed>"""
    papers = parse_arxiv(xml)
    assert papers[0]["url"] == "https://arxiv.org/abs/2401.09670"
    assert papers[0]["kind"] == "paper"
    releases = parse_github_releases(
        '[{"name":"v1","html_url":"https://github.com/kubernetes/kubernetes/releases/tag/v1","body":"'
        + ("change " * 80)
        + '","published_at":"2026-01-01T00:00:00Z"}]'
    )
    assert releases[0]["kind"] == "release"


def test_fake_pipeline_publishes_catalog_links(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIEF_FAKE_PROVIDERS", "1")
    store = Store(Settings())
    router = Router(store, fake_providers=True)
    try:
        report = ingest_records(store, router, load_fixtures())
        edition = run_edition(store, router, "2026-10-20")
    finally:
        router.close()
    assert report["saved"] == 4
    assert edition["generator"] == "fake"
    assert edition["items"]
    urls = [source["url"] for item in edition["items"] for source in item["sources"]]
    assert "https://arxiv.org/abs/2401.09670" in urls
    assert all(url.startswith("https://") for url in urls)
    for item in edition["items"]:
        item_urls = [source["url"] for source in item["sources"]]
        has_postgres = any("postgresql.org" in url for url in item_urls)
        has_inference = any("arxiv.org" in url or "microsoft.com" in url for url in item_urls)
        assert not (has_postgres and has_inference)
