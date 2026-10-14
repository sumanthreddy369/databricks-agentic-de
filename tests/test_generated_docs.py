"""Generated documents must match their sources: a stale results page or
Grafana dashboard fails CI instead of quietly disagreeing with the data."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


def test_results_page_is_current():
    import build_results_page

    committed = (REPO / "docs" / "results.md").read_text(encoding="utf-8")
    assert committed == build_results_page.build(), "run scripts/build_results_page.py"


def test_results_page_links_only_existing_images():
    import re

    page = (REPO / "docs" / "results.md").read_text(encoding="utf-8")
    images = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", page)
    assert images
    missing = [i for i in images if not (REPO / "docs" / i).exists()]
    assert missing == []
