# Senior Brief

A daily architectural briefing for one senior backend engineer. The Chrome side panel shows the edition. Each item links to the catalog URL of the blog, docs page, paper, or release it was written from.

Model calls use your own keys for xAI, OpenAI, Claude, and Gemini. Claude can write and grade. Embeddings come from xAI, OpenAI, or Gemini. The local store is SQLite with FTS5 and 768-d vectors. That is the single-node form of the AlloyDB hybrid index and the Spanner concept graph.

## Run a demo

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cd extension && npm install && npm run build && cd ..
BRIEF_FAKE_PROVIDERS=1 .venv/bin/python -m briefing
BRIEF_FAKE_PROVIDERS=1 .venv/bin/uvicorn briefing.main:app --port 8787
```

Open http://127.0.0.1:8787/panel/sidepanel.html . The demo model is local and labeled in the panel. Source buttons open the real pages.

Load `extension/` as an unpacked extension. The toolbar icon opens the side panel. The default API is `http://127.0.0.1:8787` with token `dev-token`.

## Your keys

In the side panel settings, save a provider key and set a model id per role:

| Role | Work |
|---|---|
| gate | Keep or drop a source, and name entities |
| grade, rewrite, rerank | Retrieval loop |
| synthesize, critic | The brief and the citation check |
| embed | xAI `v1`, OpenAI `text-embedding-3-large`, or Gemini `gemini-embedding-2` |

Leave `BRIEF_FAKE_PROVIDERS` unset. `POST /v1/jobs/daily` ingests the allowlist and publishes an edition. Keys are encrypted in `data/master.key` and are not returned by the API.

Set `BRIEF_AUTH=google` and `BRIEF_ALLOWED_EMAIL` when the API is not on localhost. The bearer token is then a Google ID token.

## Layout

- `briefing/` pipeline, providers, ingest, API
- `extension/` Manifest V3 side panel
- `deploy/Dockerfile` and `deploy/k8s/deployment.yaml` run one replica
- `deploy/openfang/workflow.json` is the cron step. The skill reads the token from `/var/run/brief/api-token` because OpenFang clears the process environment.
