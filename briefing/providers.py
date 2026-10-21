from __future__ import annotations

import hashlib
import json
import random
import re
from typing import Callable

import httpx

from briefing.store import PROVIDERS, ROLES, Store
from briefing.textutil import attr, fence, lexicon_entities

ChatFn = Callable[[str, str, str, str, dict], dict]


class ProviderError(Exception):
    def __init__(self, message: str, role: str = "", provider: str = "") -> None:
        super().__init__(message)
        self.role = role
        self.provider = provider


class MissingCredential(ProviderError):
    pass


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ProviderError("model did not return JSON")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ProviderError("model JSON was not an object")
    return value


def _post(client: httpx.Client, url: str, headers: dict, payload: dict) -> httpx.Response:
    response = client.post(url, headers=headers, json=payload)
    if response.status_code == 429:
        response = client.post(url, headers=headers, json=payload)
    return response


class OpenAICompatAdapter:
    def __init__(self, name: str, base_url: str, prefix_embeddings: bool = False) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.prefix_embeddings = prefix_embeddings

    def chat(self, client: httpx.Client, api_key: str, model: str, system: str, user: str, schema: dict) -> dict:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "strict": False, "schema": schema},
            },
        }
        response = _post(
            client,
            self.base_url + "/chat/completions",
            {"Authorization": "Bearer " + api_key},
            payload,
        )
        if response.status_code == 400:
            payload["response_format"] = {"type": "json_object"}
            response = _post(
                client,
                self.base_url + "/chat/completions",
                {"Authorization": "Bearer " + api_key},
                payload,
            )
        if response.status_code >= 400:
            raise ProviderError(self.name + " chat failed: " + response.text[:300], provider=self.name)
        content = response.json()["choices"][0]["message"].get("content") or ""
        return parse_json_object(content)

    def embed(
        self, client: httpx.Client, api_key: str, model: str, texts: list[str], kind: str, dimensions: int
    ) -> list[list[float]]:
        inputs = texts
        if self.prefix_embeddings:
            prefix = "query: " if kind == "query" else "passage: "
            inputs = [prefix + text for text in texts]
        payload = {"model": model, "input": inputs, "dimensions": dimensions, "encoding_format": "float"}
        response = _post(
            client,
            self.base_url + "/embeddings",
            {"Authorization": "Bearer " + api_key},
            payload,
        )
        if response.status_code == 400 and "dimensions" in payload:
            payload.pop("dimensions")
            response = _post(
                client,
                self.base_url + "/embeddings",
                {"Authorization": "Bearer " + api_key},
                payload,
            )
        if response.status_code >= 400:
            raise ProviderError(self.name + " embeddings failed: " + response.text[:300], provider=self.name)
        data = response.json()["data"]
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [_fit(item["embedding"], dimensions) for item in ordered]


class AnthropicAdapter:
    name = "anthropic"

    def chat(self, client: httpx.Client, api_key: str, model: str, system: str, user: str, schema: dict) -> dict:
        payload = {
            "model": model,
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": "emit",
                    "description": "Return the structured result.",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": "emit"},
        }
        response = _post(
            client,
            "https://api.anthropic.com/v1/messages",
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            payload,
        )
        if response.status_code >= 400:
            raise ProviderError("anthropic chat failed: " + response.text[:300], provider=self.name)
        for block in response.json().get("content") or []:
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                return block["input"]
            if block.get("type") == "text":
                return parse_json_object(block.get("text") or "")
        raise ProviderError("anthropic response had no tool result", provider=self.name)

    def embed(self, client: httpx.Client, api_key: str, model: str, texts: list[str], kind: str, dimensions: int):
        raise ProviderError(
            "Claude has no embedding model. Save an xAI, OpenAI, or Gemini key for the embed role.",
            provider=self.name,
        )


class GeminiAdapter:
    name = "gemini"

    def chat(self, client: httpx.Client, api_key: str, model: str, system: str, user: str, schema: dict) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": _gemini_schema(schema),
            },
        }
        response = _post(client, url, {"x-goog-api-key": api_key}, payload)
        if response.status_code == 400:
            payload["generationConfig"] = {"responseMimeType": "application/json"}
            response = _post(client, url, {"x-goog-api-key": api_key}, payload)
        if response.status_code >= 400:
            raise ProviderError("gemini chat failed: " + response.text[:300], provider=self.name)
        parts = response.json()["candidates"][0]["content"]["parts"]
        return parse_json_object(parts[0].get("text") or "")

    def embed(
        self, client: httpx.Client, api_key: str, model: str, texts: list[str], kind: str, dimensions: int
    ) -> list[list[float]]:
        model_name = model if model.startswith("models/") else "models/" + model
        bare = model_name.split("/", 1)[1]
        task = "RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{bare}:batchEmbedContents"
        payload = {
            "requests": [
                {
                    "model": model_name,
                    "content": {"parts": [{"text": text}]},
                    "outputDimensionality": dimensions,
                    "taskType": task,
                }
                for text in texts
            ]
        }
        response = _post(client, url, {"x-goog-api-key": api_key}, payload)
        if response.status_code >= 400:
            raise ProviderError("gemini embeddings failed: " + response.text[:300], provider=self.name)
        embeddings = response.json().get("embeddings") or []
        return [_fit(item.get("values") or [], dimensions) for item in embeddings]


def _fit(vector: list[float], dimensions: int) -> list[float]:
    if len(vector) > dimensions:
        vector = vector[:dimensions]
    if len(vector) != dimensions:
        raise ProviderError("embedding dimension was " + str(len(vector)) + ", expected " + str(dimensions))
    return [float(value) for value in vector]


def _gemini_schema(schema: dict) -> dict:
    cleaned = {}
    for key, value in schema.items():
        if key in {"$schema", "additionalProperties"}:
            continue
        if isinstance(value, dict):
            cleaned[key] = _gemini_schema(value)
        elif isinstance(value, list):
            cleaned[key] = [_gemini_schema(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value
    return cleaned


def _fake_vector(text: str, dimensions: int = 768) -> list[float]:
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)
    values = [rng.uniform(-1, 1) for _ in range(dimensions)]
    norm = sum(value * value for value in values) ** 0.5 or 1.0
    return [value / norm for value in values]


class FakeAdapter:
    name = "fake"

    def chat(self, client: httpx.Client, api_key: str, model: str, system: str, user: str, schema: dict) -> dict:
        properties = schema.get("properties") or {}
        if "keep" in properties and "signal" in properties:
            names = lexicon_entities(user) or ["prefill"]
            return {
                "keep": True,
                "reason": "tradeoff in the source",
                "signal": "tradeoff",
                "entities": [{"name": name} for name in names[:6]],
            }
        if "entities" in properties:
            return {"entities": [{"name": "prefill"}, {"name": "logical decoding"}]}
        if "slots" in properties:
            return {
                "slots": [
                    {"question": "prefill decode kv-cache disaggregation transfer backpressure"},
                    {"question": "logical decoding replication slot confirmed flush LSN"},
                    {"question": "admission control load shedding cascading failure"},
                ]
            }
        if "grades" in properties:
            question_match = re.search(r"Question: (.+)", user)
            question = question_match.group(1).lower() if question_match else ""
            question_tokens = set(re.findall(r"[a-z0-9]{4,}", question))
            grades = []
            for source_id, body in re.findall(
                r'source_id="([^"]+)"[^>]*>(.*?)</evidence>', user, flags=re.S
            ):
                haystack = body.lower()
                overlap = sum(
                    1
                    for token in question_tokens
                    if re.search(r"\b" + re.escape(token) + r"\b", haystack)
                )
                if overlap >= 2:
                    grades.append({"source_id": source_id, "verdict": "sufficient", "missing_facet": None})
                else:
                    grades.append(
                        {
                            "source_id": source_id,
                            "verdict": "insufficient",
                            "missing_facet": "mechanism",
                        }
                    )
            return {"grades": grades}
        if "query" in properties and "verdict" not in properties and "items" not in properties:
            question_match = re.search(r"Question: (.+)", user)
            question = question_match.group(1).split("\n", 1)[0].strip() if question_match else ""
            return {"query": question or "prefill decode kv-cache"}
        if set(properties) == {"keep"} or (
            "keep" in properties and "signal" not in properties and "grades" not in properties
        ):
            count = len(re.findall(r"^\[(\d+)\]", user, flags=re.M))
            return {"keep": list(range(1, min(count, 6) + 1))}
        if "strikes" in properties:
            return {"verdict": "pass", "strikes": []}
        if "items" in properties:
            return _fake_brief(user)
        if "ok" in properties:
            return {"ok": True}
        raise ProviderError("fake provider has no fixture for this schema")

    def embed(self, client, api_key, model, texts, kind, dimensions):
        return [_fake_vector(kind + ":" + text, dimensions) for text in texts]


ADAPTERS = {
    "openai": OpenAICompatAdapter("openai", "https://api.openai.com/v1"),
    "xai": OpenAICompatAdapter("xai", "https://api.x.ai/v1", prefix_embeddings=True),
    "anthropic": AnthropicAdapter(),
    "gemini": GeminiAdapter(),
    "fake": FakeAdapter(),
}


def _fake_brief(user: str) -> dict:
    date_match = re.search(r"Edition date: (\d{4}-\d{2}-\d{2})", user)
    edition_date = date_match.group(1) if date_match else "1970-01-01"
    slots = re.findall(r'<slot id="(\d+)" question="([^"]*)">(.*?)</slot>', user, flags=re.S)
    items = []
    for slot_id, question, body in slots:
        evidences = re.findall(
            r'<evidence source_id="([^"]+)" title="([^"]*)" url="[^"]*" published="[^"]*">(.*?)</evidence>',
            body,
            flags=re.S,
        )
        if len(evidences) < 2:
            continue
        first = " ".join(evidences[0][2].split())
        sentences = [part.strip() for part in re.split(r"(?<=[.])\s+", first) if part.strip()]
        while len(sentences) < 4:
            sentences.append("not reported")
        items.append(
            {
                "title": evidences[0][1][:120] or ("Slot " + slot_id),
                "decision": sentences[0],
                "mechanism": sentences[1],
                "what_breaks": sentences[2],
                "what_to_copy": sentences[3],
                "citations": [item[0] for item in evidences],
            }
        )
    if not items and "question=" in user:
        question = "evidence was too thin"
    else:
        question = ""
    dropped = [{"claim": question, "reason": "insufficient evidence"}] if question else []
    return {"edition_date": edition_date, "items": items, "dropped": dropped}


class Router:
    def __init__(self, store: Store, fake_providers: bool, client: httpx.Client | None = None) -> None:
        self.store = store
        self.fake_providers = fake_providers
        self.client = client or httpx.Client(timeout=httpx.Timeout(150.0, connect=15.0))

    def close(self) -> None:
        self.client.close()

    def _selection(self, role: str) -> tuple[str, str]:
        if role not in ROLES and role != "embed":
            raise ProviderError("unknown role", role=role)
        config = self.store.role_config()
        if role == "embed":
            selected = config["embed"]
        else:
            selected = config["roles"][role]
        provider = (selected.get("provider") or "").strip()
        model = (selected.get("model") or "").strip()
        if provider and model:
            if provider not in ADAPTERS:
                raise ProviderError("unknown provider " + provider, role=role, provider=provider)
            return provider, model
        if self.fake_providers and not self._has_real_key():
            return "fake", "fake-" + role
        raise MissingCredential(
            "Save a provider and model for the " + role + " role.",
            role=role,
        )

    def _has_real_key(self) -> bool:
        return any(self.store.api_key(name) for name in ("xai", "openai", "anthropic", "gemini"))

    def _key(self, provider: str, role: str) -> str:
        if provider == "fake":
            return "fake"
        key = self.store.api_key(provider)
        if not key:
            raise MissingCredential(
                "No API key stored for " + provider + ".",
                role=role,
                provider=provider,
            )
        return key

    def chat(self, role: str, system: str, user: str, schema: dict) -> dict:
        import sys
        import time

        provider, model = self._selection(role)
        key = self._key(provider, role)
        started = time.monotonic()
        sys.stderr.write("brief chat start role={role} model={model} chars={n}\n".format(role=role, model=model, n=len(user)))
        sys.stderr.flush()
        try:
            result = ADAPTERS[provider].chat(self.client, key, model, system, user, schema)
        except ProviderError as exc:
            exc.role = exc.role or role
            exc.provider = exc.provider or provider
            sys.stderr.write("brief chat fail role={role} seconds={s:.1f} error={err}\n".format(role=role, s=time.monotonic() - started, err=str(exc)[:180]))
            sys.stderr.flush()
            raise
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            sys.stderr.write("brief chat fail role={role} seconds={s:.1f} error={err}\n".format(role=role, s=time.monotonic() - started, err=type(exc).__name__))
            sys.stderr.flush()
            raise ProviderError(provider + " chat timed out", role=role, provider=provider) from exc
        sys.stderr.write("brief chat done role={role} seconds={s:.1f}\n".format(role=role, s=time.monotonic() - started))
        sys.stderr.flush()
        return result

    def embed(self, texts: list[str], kind: str) -> tuple[str, list[list[float]]]:
        provider, model = self._selection("embed")
        if provider == "anthropic":
            raise ProviderError(
                "Claude has no embedding model. Choose xAI, OpenAI, or Gemini for embed.",
                role="embed",
                provider=provider,
            )
        key = self._key(provider, "embed")
        dimensions = 768
        vectors = ADAPTERS[provider].embed(self.client, key, model, texts, kind, dimensions)
        return f"{provider}:{model}:{dimensions}", vectors

    def ping(self, provider: str, api_key: str, model: str, embed: bool = False) -> None:
        if provider not in PROVIDERS:
            raise ProviderError("unknown provider", provider=provider)
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        ADAPTERS[provider].chat(
            self.client,
            api_key,
            model,
            "Reply with the required JSON object only.",
            "Confirm the key works.",
            schema,
        )
        if embed:
            if provider == "anthropic":
                raise ProviderError("Claude has no embedding model.", provider=provider)
            ADAPTERS[provider].embed(self.client, api_key, model, ["ping"], "passage", 768)


def evidence_block(parent: dict) -> str:
    return (
        '<evidence source_id="{sid}" title="{title}" url="{url}" published="{published}">\n'
        "{text}\n</evidence>"
    ).format(
        sid=attr(parent["doc_id"]),
        title=attr(parent["title"]),
        url=attr(parent["url"]),
        published=attr(parent.get("published_at") or ""),
        text=fence(parent["text"][:4000]),
    )
