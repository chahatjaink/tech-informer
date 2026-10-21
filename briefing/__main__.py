from __future__ import annotations

import os


def run_demo() -> None:
    os.environ.setdefault("BRIEF_FAKE_PROVIDERS", "1")
    from briefing.ingest import ingest_records, load_fixtures
    from briefing.main import create_app
    from briefing.pipeline import run_edition
    from briefing.providers import Router
    from briefing.store import Store

    create_app()
    store = Store()
    router = Router(store, fake_providers=True)
    try:
        report = ingest_records(store, router, load_fixtures())
        edition = run_edition(store, router)
    finally:
        router.close()
    print("ingest", report)
    print("edition", edition["edition_id"], "items", len(edition["items"]))
    for item in edition["items"]:
        print("-", item["title"])
        for source in item["sources"]:
            print("  ", source["kind"], source["url"])


if __name__ == "__main__":
    run_demo()
