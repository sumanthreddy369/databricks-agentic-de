"""Runbook search: which catalog problem is this, and what do I do?

Agent Bricks' Knowledge Assistant would be the managed way to do this, but
it is a legacy feature that new accounts can no longer create (found when
creating one on Free Edition), so the same retrieval-plus-answer is built
from Databricks-hosted models directly:

- retrieval: the runbook pages (docs/runbooks/, generated from the problem
  catalog) embedded with a Foundation Model API embedding endpoint
  (`databricks-gte-large-en`), nearest pages by cosine similarity;
- answer: the top pages handed to a hosted chat model (agent/databricks_llm.py),
  which must start with the problem ID it picked.

Both calls are injectable, so tests run with no network.
"""

import hashlib
import json
import math
import re
from collections.abc import Callable
from pathlib import Path

import httpx
import tenacity

RUNBOOK_DIR = Path(__file__).resolve().parent.parent / "docs" / "runbooks"
# Page embeddings keyed by a hash of the page text (gitignored): only changed
# pages are embedded again - the free endpoint is rate-limited.
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "cache" / "runbook_embeddings.json"
EMBEDDING_ENDPOINT = "databricks-gte-large-en"
ANSWER_PROMPT = (
    "You answer on-call questions about a healthcare lakehouse from the runbook pages below, and only from them. "
    "Pick the single page that best matches the question. Start your answer with 'Problem <ID>:' using that "
    "page's Problem ID (for example 'Problem B1:'), then say what to do and the autonomy level, in at most "
    "five sentences. If no page fits, start with 'Problem none:' and say so."
)
_CITED = re.compile(r"Problem\s+([A-O]\d{1,2}|none)\b")

Embedder = Callable[[list[str]], list[list[float]]]


def load_pages(directory: Path = RUNBOOK_DIR) -> dict[str, str]:
    """{problem_id: page text}."""
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.md"))}


def databricks_embedder(profile: str | None = None, *, config=None, transport=None) -> Embedder:
    if config is None:
        from databricks.sdk.core import Config

        config = Config(profile=profile) if profile else Config()
    client = httpx.Client(timeout=60, transport=transport)
    url = f"{config.host.rstrip('/')}/serving-endpoints/{EMBEDDING_ENDPOINT}/invocations"

    @tenacity.retry(
        retry=tenacity.retry_if_exception(
            lambda e: (
                isinstance(e, httpx.TransportError)
                or (isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (429, 500, 502, 503, 504))
            )
        ),
        stop=tenacity.stop_after_attempt(6),
        wait=tenacity.wait_exponential(multiplier=2, max=60),
        reraise=True,
    )
    def _batch(texts: list[str]) -> list[list[float]]:
        response = client.post(url, headers=config.authenticate(), json={"input": texts})
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda r: r.get("index", 0))  # stable if no index
        return [row["embedding"] for row in rows]

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(texts), 8):
            vectors += _batch(texts[start : start + 8])
        return vectors

    return embed


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


def focus(page: str) -> str:
    """The part of a page worth embedding: title, what happens, detection and
    action. Headings and the autonomy/status sections are the same on every
    page and only blur the vectors (seen in the first evaluation run)."""
    kept = []
    for line in page.splitlines():
        if line.startswith("## Autonomy"):
            break
        if line.startswith("# "):
            kept.append(line[2:])
        elif line and not line.startswith("#") and not line.startswith("Problem ID:"):
            kept.append(line)
    return " ".join(kept)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RunbookIndex:
    def __init__(
        self,
        pages: dict[str, str],
        embed: Embedder,
        cache_path: Path | None = None,
        embed_query: Embedder | None = None,
    ) -> None:
        self.pages = pages
        self._embed = embed
        # Some models embed questions differently from documents (nomic: task prefixes).
        self._embed_query = embed_query or embed
        self._ids = list(pages)
        texts = {pid: focus(pages[pid]) for pid in self._ids}
        cache = json.loads(cache_path.read_text()) if cache_path and cache_path.exists() else {}
        missing = [pid for pid in self._ids if _key(texts[pid]) not in cache]
        for pid, vector in zip(missing, embed([texts[pid] for pid in missing]) if missing else []):
            cache[_key(texts[pid])] = vector
        if cache_path and missing:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({_key(texts[pid]): cache[_key(texts[pid])] for pid in self._ids}))
        self._vectors = [_unit(cache[_key(texts[pid])]) for pid in self._ids]

    def search(self, question: str, k: int = 3) -> list[tuple[str, float]]:
        q = _unit(self._embed_query([question])[0])
        scored = [(pid, sum(a * b for a, b in zip(q, v))) for pid, v in zip(self._ids, self._vectors)]
        return sorted(scored, key=lambda item: -item[1])[:k]

    def hybrid(
        self, question: str, k: int = 5, keyword: Callable[[str, int], list[str]] | None = None
    ) -> list[str]:
        """Reciprocal rank fusion of embedding search and the catalog's
        keyword search (lookup_problem's ranking): each finds pages the other
        misses. RRF score = sum of 1 / (60 + rank) over the two lists."""
        if keyword is None:
            from agent.tools.problem_catalog import search

            def keyword(q, n):
                return [p["id"] for p in search(q, limit=n)]

        depth = 20
        ranked = {
            "embedding": [pid for pid, _ in self.search(question, depth)],
            "keyword": [pid for pid in keyword(question, depth) if pid in self.pages],
        }
        fused: dict[str, float] = {}
        for ids in ranked.values():
            for rank, pid in enumerate(ids, 1):
                fused[pid] = fused.get(pid, 0.0) + 1.0 / (60 + rank)
        return sorted(fused, key=lambda pid: -fused[pid])[:k]

    def answer(self, question: str, model, k: int = 5) -> tuple[str, list[str]]:
        """(answer text, retrieved ids): the top `k` hybrid results handed to
        `model` (anything with run_tool_loop: Claude or a DatabricksModel);
        no tools - the pages are the only context."""
        hits = self.hybrid(question, k)
        context = "\n\n---\n\n".join(self.pages[pid] for pid in hits)
        messages = [{"role": "user", "content": f"Runbook pages:\n\n{context}\n\nQuestion: {question}"}]
        text = model.run_tool_loop(ANSWER_PROMPT, messages, [], lambda name, args: None, max_turns=1)
        return text, hits


def cited_problem(answer: str) -> str | None:
    """The problem ID an answer starts from ('Problem B1:'), or None."""
    match = _CITED.search(answer or "")
    return None if match is None or match.group(1) == "none" else match.group(1)
