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


# The size and network claims below are numbers the code can outgrow. Each test reads the number
# from the README and the fact from the repository, so a change to either one fails here.

REPO = Path(__file__).resolve().parent.parent
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}


def test_the_line_count_is_within_five_percent_of_the_installed_code():
    claimed = int(re.search(r"About ([\d,]+) lines of Python", README).group(1).replace(",", ""))
    files = [*(REPO / "trioditis").glob("*.py"), *(REPO / "trioditis_metrics").glob("*.py")]
    actual = sum(len(f.read_text(encoding="utf-8").splitlines()) for f in files)
    assert abs(actual - claimed) <= 0.05 * actual, f"README says about {claimed:,} lines; the two packages have {actual:,}"


def test_the_dependency_counts_match_the_lockfile():
    direct = [ln for ln in (REPO / "requirements.in").read_text(encoding="utf-8").splitlines()
              if ln.strip() and not ln.lstrip().startswith(("#", "-"))]
    pinned = re.findall(r"(?m)^[A-Za-z0-9_.-]+==", (REPO / "requirements.txt").read_text(encoding="utf-8"))
    claims = re.findall(r"(\w+) direct dependencies, (\d+) packages in the lockfile", README)
    assert len(claims) == 2  # the front table and the security table
    for word, total in claims:
        assert NUMBER_WORDS[word.lower()] == len(direct)
        assert int(total) == len(pinned)


def test_the_background_intervals_are_the_ones_in_the_code():
    from trioditis.accounts import ACCOUNTS_EVERY_S
    from trioditis.app import HARDWARE_EVERY_S
    assert f"`GET /api/tags`, every {HARDWARE_EVERY_S // 60} minutes" in README
    assert f"own account API**, every {ACCOUNTS_EVERY_S // 60} minutes" in README


def test_no_sponsor_button_is_promised_without_a_funding_file():
    assert (REPO / ".github" / "FUNDING.yml").exists() or "Sponsor" not in README
