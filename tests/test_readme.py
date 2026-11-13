"""The README is also the PyPI project page, and PyPI does not resolve relative addresses."""
import re
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_every_link_and_image_is_absolute_or_an_anchor():
    targets = re.findall(r"\]\(([^)\s]+)\)", README) + re.findall(r'(?:src|srcset|href)="([^"]+)"', README)
    assert len(targets) > 20
    relative = [t for t in targets if not t.startswith(("https://", "#"))]
    assert relative == [], f"relative addresses break on pypi.org: {relative}"


def test_absolute_addresses_into_the_repository_name_files_that_exist():
    repo = Path(__file__).resolve().parent.parent
    paths = re.findall(r"https://(?:github\.com/alexb-joat/trioditis/blob|raw\.githubusercontent\.com/alexb-joat/trioditis)/main/([^)\"#\s]+)", README)
    assert len(paths) > 15
    missing = [p for p in paths if not (repo / p).exists()]
    assert missing == [], f"the README points at files that are not in the repository: {missing}"
