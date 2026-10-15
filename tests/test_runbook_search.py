"""Runbook pages and the search over them (agent/runbook_search.py), with a
fake embedder and model - no network."""

import json
import sys
from pathlib import Path

from agent.runbook_search import RunbookIndex, cited_problem, load_pages

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import build_runbooks  # noqa: E402


def _bag_embed(texts):
    """Word-presence vectors over a tiny vocabulary: enough to rank pages."""
    vocab = ["lag", "behind", "999", "sentinel", "duplicate", "twice", "identity", "patient"]
    return [[float(w in t.lower()) + 0.01 for w in vocab] for t in texts]


def _page(pid: str, title: str, what: str) -> str:
    return chr(10).join(
        [
            f"# {pid}: {title}",
            "",
            f"Problem ID: {pid}.",
            "",
            "## What happens",
            "",
            what,
            "",
            "## Autonomy",
            "",
            "L1",
        ]
    )


PAGES = {
    "A1": _page("A1", "Consumer lag", "Dashboards fall behind."),
    "M4": _page("M4", "Sentinel values", "999 in vitals."),
    "A4": _page("A4", "Duplicate events", "The same reading twice."),
}


def test_every_catalog_problem_has_a_runbook_and_they_are_current():
    assert len(build_runbooks.pages()) == 104
    assert build_runbooks.main(["--check"]) == 0


def test_a_page_carries_its_id_detection_action_and_autonomy():
    page = load_pages()["B1"]
    assert page.startswith("# B1: ") and "Problem ID: B1." in page
    assert "## How to detect it" in page and "## What to do" in page and "Level L2" in page


def test_search_ranks_the_page_that_matches(tmp_path):
    index = RunbookIndex(PAGES, _bag_embed, cache_path=tmp_path / "cache.json")
    assert index.search("heart rate shows 999", k=1)[0][0] == "M4"
    assert index.search("the same reading arrived twice", k=1)[0][0] == "A4"


def test_page_embeddings_are_cached_and_only_changed_pages_re_embedded(tmp_path):
    calls = []

    def counting(texts):
        calls.append(len(texts))
        return _bag_embed(texts)

    cache = tmp_path / "cache.json"
    RunbookIndex(PAGES, counting, cache_path=cache)
    # A change outside the embedded part (autonomy) needs no new embedding...
    RunbookIndex({**PAGES, "M4": PAGES["M4"] + " Changed."}, counting, cache_path=cache)
    # ...a change to what happens does.
    changed = {**PAGES, "M4": _page("M4", "Sentinel values", "999 or -1 in vitals.")}
    RunbookIndex(changed, counting, cache_path=cache)
    assert calls == [3, 1]
    assert len(json.loads(cache.read_text())) == 3


def test_answer_is_built_from_the_retrieved_pages_only(tmp_path):
    seen = {}

    class Model:
        def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
            seen["context"] = messages[0]["content"]
            assert tools == [] and max_turns == 1
            return "Problem M4: treat 999 as missing, not as a reading."

    index = RunbookIndex(PAGES, _bag_embed, cache_path=tmp_path / "c.json")
    answer, hits = index.answer("heart rate 999", Model(), k=1)
    assert hits[0] == "M4" and "Sentinel values" in seen["context"] and "Consumer lag" not in seen["context"]
    assert cited_problem(answer) == "M4"


def test_cited_problem_reads_the_answer_prefix_not_autonomy_levels():
    assert cited_problem("Problem L8: use as_of. Autonomy L3.") == "L8"
    assert cited_problem("Autonomy level L1 applies. Problem B1: quarantine.") == "B1"
    assert cited_problem("Problem none: no runbook fits.") is None
    assert cited_problem("") is None


def test_focus_drops_the_boilerplate_every_page_shares():
    from agent.runbook_search import focus

    text = focus(load_pages()["B1"])
    assert text.startswith("B1: Data-quality drop spike")
    assert "Autonomy" not in text and "Status in this project" not in text and "##" not in text


def test_hybrid_fuses_keyword_and_embedding_rankings(tmp_path):
    index = RunbookIndex(PAGES, _bag_embed, cache_path=tmp_path / "c.json")
    # Embedding alone ranks M4 first for "999"; a keyword list that puts A4
    # first (and M4 second) pulls A4 up beside it - each list contributes.
    fused = index.hybrid("heart rate shows 999", k=3, keyword=lambda q, n: ["A4", "M4"])
    assert set(fused[:2]) == {"M4", "A4"}
