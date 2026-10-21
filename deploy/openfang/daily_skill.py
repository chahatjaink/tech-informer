#!/usr/bin/env python3
"""OpenFang skill. Reads the API token from a file because the runtime clears the environment."""

import json
import sys
import urllib.request
from pathlib import Path

payload = json.load(sys.stdin)
tool = payload.get("tool")
if tool != "daily":
    json.dump({"error": "unknown tool"}, sys.stdout)
    sys.exit(0)

data = payload.get("input") or {}
base = str(data.get("api_base") or "http://127.0.0.1:8787").rstrip("/")
token_path = Path(str(data.get("token_file") or "/var/run/brief/api-token"))
token = token_path.read_text(encoding="utf-8").strip()
request = urllib.request.Request(
    base + "/v1/jobs/daily",
    method="POST",
    headers={"Authorization": "Bearer " + token},
)
with urllib.request.urlopen(request, timeout=900) as response:
    json.dump({"result": response.read().decode("utf-8")}, sys.stdout)
