from fastapi.testclient import TestClient


def test_api_demo_edition(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIEF_FAKE_PROVIDERS", "1")
    monkeypatch.setenv("BRIEF_API_TOKEN", "test-token")
    from briefing.main import create_app

    client = TestClient(create_app())
    assert client.get("/v1/editions/latest").status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    created = client.post("/v1/jobs/demo", headers=headers)
    assert created.status_code == 200, created.text
    latest = client.get("/v1/editions/latest", headers=headers)
    assert latest.status_code == 200
    body = latest.json()
    assert body["items"][0]["sources"][0]["url"].startswith("https://")
    saved = client.post(
        "/v1/credentials",
        headers=headers,
        json={"provider": "openai", "api_key": "sk-test-secret-value"},
    )
    assert saved.status_code == 200
    assert "sk-test-secret-value" not in saved.text
    assert saved.json()["last4"] == "alue"
