from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
STOPWORDS = {
    "a", "an", "and", "are", "cua", "co", "cho", "duoc", "gi", "how", "is", "la",
    "mot", "of", "server", "the", "the", "to", "toi", "trong", "va", "voi", "you",
}


@dataclass(frozen=True)
class KnowledgeChunk:
    source: str
    heading: str
    text: str
    tokens: frozenset[str]


@dataclass(frozen=True)
class SearchResult:
    chunk: KnowledgeChunk
    score: float


def tokenize(text: str) -> set[str]:
    text = unicodedata.normalize("NFKD", text.casefold()).replace("đ", "d")
    text = "".join(character for character in text if not unicodedata.combining(character))
    return {
        word for word in WORD_RE.findall(text)
        if len(word) > 1 and word not in STOPWORDS
    }


def _split_document(source: str, content: str, max_chars: int = 2400) -> list[KnowledgeChunk]:
    chunks: list[KnowledgeChunk] = []
    heading = source
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if text:
            chunks.append(KnowledgeChunk(source, heading, text, frozenset(tokenize(heading + " " + text))))
        buffer = []

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if line.startswith("#"):
            flush()
            heading = line.lstrip("# ").strip() or source
            continue
        if sum(len(part) + 1 for part in buffer) + len(line) > max_chars:
            flush()
        buffer.append(line)
    flush()
    return chunks


class KnowledgeBase:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.chunks: list[KnowledgeChunk] = []
        self.reload()

    def reload(self) -> int:
        self.directory.mkdir(parents=True, exist_ok=True)
        chunks: list[KnowledgeChunk] = []
        seen: set[str] = set()
        for path in sorted(self.directory.rglob("*")):
            if path.is_symlink() or path.suffix.lower() not in {".md", ".txt"} or not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            source = path.relative_to(self.directory).as_posix()
            for chunk in _split_document(source, content):
                fingerprint = re.sub(r"\s+", " ", chunk.text).casefold().strip()
                if fingerprint not in seen:
                    chunks.append(chunk)
                    seen.add(fingerprint)
        self.chunks = chunks
        return len(chunks)

    def search(self, query: str, limit: int = 5) -> list[SearchResult]:
        query_tokens = tokenize(query)
        if not query_tokens or not self.chunks:
            return []
        document_frequency = {
            token: sum(token in chunk.tokens for chunk in self.chunks)
            for token in query_tokens
        }
        results: list[SearchResult] = []
        normalized_query = query.casefold().strip()
        for chunk in self.chunks:
            overlap = query_tokens.intersection(chunk.tokens)
            if not overlap:
                continue
            score = sum(
                math.log((len(self.chunks) + 1) / (document_frequency[token] + 0.5)) + 1
                for token in overlap
            )
            score *= len(overlap) / len(query_tokens)
            if normalized_query and normalized_query in chunk.text.casefold():
                score += 3
            results.append(SearchResult(chunk, score))
        results.sort(key=lambda result: result.score, reverse=True)
        return results[:limit]
