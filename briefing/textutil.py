from __future__ import annotations

import hashlib
import math
import re
import array
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")
HEADING_RE = re.compile(r"(?m)^(#{1,3}\s+.+)$")
TRACKING = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower() or "https"
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in TRACKING and not key.lower().startswith("utm_")
    ]
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def doc_id_for(url: str) -> str:
    return "d_" + hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def pack_vector(values: list[float]) -> bytes:
    return array.array("f", values).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(blob)
    return list(values)


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def fts_query(text: str, limit: int = 12) -> str:
    tokens = []
    seen = set()
    for token in TOKEN_RE.findall(text.lower()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= limit:
            break
    return " OR ".join(tokens)


def rrf(rankings: list[list[str]], k: int = 60) -> list[str]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for index, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + index + 1)
    return sorted(scores, key=lambda item: scores[item], reverse=True)


def heuristic_keep(title: str, text: str) -> bool:
    title_l = title.lower()
    blocked = ("beginner", "getting started", "introduction to", "tutorial", "what is ")
    if any(phrase in title_l for phrase in blocked):
        return False
    return len(text.strip()) >= 400


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
            return
        if tag in {"h1", "h2", "h3"}:
            level = int(tag[1])
            self.parts.append("\n\n" + ("#" * level) + " ")
        elif tag in {"p", "li", "br", "tr", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
            return
        if tag in {"h1", "h2", "h3", "p", "li"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sections(markdown: str) -> list[tuple[str, str]]:
    parts = HEADING_RE.split(markdown.strip())
    sections: list[tuple[str, str]] = []
    if parts and parts[0].strip():
        sections.append(("Overview", parts[0].strip()))
    index = 1
    while index < len(parts):
        heading = parts[index].lstrip("#").strip()
        body = parts[index + 1].strip() if index + 1 < len(parts) else ""
        if body:
            sections.append((heading, body))
        index += 2
    if not sections and markdown.strip():
        sections.append(("Overview", markdown.strip()))
    return sections


def _paragraph_groups(body: str, limit: int = 1600) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    if not paragraphs:
        return [body[:limit]]
    groups: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else current + "\n\n" + paragraph
        if current and len(candidate) > limit:
            groups.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


def chunk_document(title: str, markdown: str) -> list[dict]:
    chunks = []
    ordinal = 0
    for section, body in split_sections(markdown):
        parent = (section + "\n\n" + body).strip()[:8000]
        parent_id = section
        for child in _paragraph_groups(body):
            chunks.append(
                {
                    "ordinal": ordinal,
                    "section_path": section,
                    "parent_id": parent_id,
                    "text": child[:1600],
                    "parent_text": parent,
                    "index_text": f"{title}\n{section}\n{child[:1600]}",
                }
            )
            ordinal += 1
    return chunks


def attr(value: str) -> str:
    return (
        value.replace("&", " ")
        .replace('"', "")
        .replace("<", " ")
        .replace(">", " ")
        .replace("\n", " ")
        .strip()
    )


def fence(text: str) -> str:
    return text.replace("</", "< /")


LEXICON = (
    "prefill",
    "decode",
    "kv-cache",
    "kv cache",
    "logical decoding",
    "replication slot",
    "admission control",
    "load shedding",
    "consistent hashing",
    "cascading failure",
    "kubernetes",
    "postgres",
    "raft",
    "kafka",
    "spanner",
)


def lexicon_entities(text: str) -> list[str]:
    lowered = text.lower()
    found = []
    for name in LEXICON:
        if re.search(r"\b" + re.escape(name) + r"\b", lowered) and name not in found:
            found.append(name)
    return found[:8]
