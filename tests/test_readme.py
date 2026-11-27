"""The README is also the PyPI project page, and PyPI does not resolve relative addresses."""
import re
from pathlib import Path

import pytest

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


def help_measure_block() -> str:
    parts = re.split(r"(?m)^## ", README)
    [block] = [p for p in parts if p.startswith("Help measure")]
    return block


def cli_help(*argv: str, capsys) -> tuple[int, str]:
    from trioditis.__main__ import main
    with pytest.raises(SystemExit) as e:
        main([*argv, "--help"])
    return e.value.code, capsys.readouterr().out


def test_the_help_measure_block_names_a_registered_command_and_flags_that_exist(capsys):
    block = help_measure_block()
    commands = re.findall(r"(?m)^trioditis ([a-z-]+)", block)
    assert commands == ["results"]
    code, help_text = cli_help(*commands, capsys=capsys)
    assert code == 0
    flags = set(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", block))
    assert {"--model", "--base-url", "--out", "--own-labels", "--answers", "--no-share"} <= flags
    missing = sorted(f for f in flags if f not in help_text)
    assert missing == [], f"the README names options `trioditis results` does not have: {missing}"


def test_the_help_measure_block_would_fail_on_an_unknown_command_or_flag(capsys):
    code, _ = cli_help("no-such-command", capsys=capsys)
    assert code == 2                                     # an unregistered command exits 2, so the check above can fail
    _, help_text = cli_help("results", capsys=capsys)
    assert "--no-such-flag" not in help_text


def test_the_help_measure_block_names_the_models_the_program_asks_for():
    from trioditis import measure
    from trioditis.__main__ import DEFAULT_PICKER
    from trioditis.config import load_config
    block = help_measure_block()
    assert f"`{DEFAULT_PICKER}`" in block
    assert f"`{load_config(measure.shipped_config_path()).embeddings.model}`" in block


def test_the_help_measure_block_keeps_the_promises_the_program_keeps():
    block = help_measure_block()
    assert "default is No" in block and "CC0" in block and "nothing is sent before that" in block
    assert "only to the local server" in block and "never holds prompt text" in block
    assert block.count("```") == 2, "one command, no pictures, no tables of figures"
    assert not re.search(r"\d+(\.\d+)?\s*%|\bpassed\b|\b\d+/\d+\b", block), "no figures from model runs in this block"
    assert README.count("## Help measure") == 1, "one block; the first screen only points at it"


def test_no_sponsor_button_is_promised_without_a_funding_file():
    assert (REPO / ".github" / "FUNDING.yml").exists() or "Sponsor" not in README


def first_screen() -> str:
    return README.split("\n## Quick start", 1)[0]


def test_the_first_screen_points_at_the_one_help_measure_block():
    top = first_screen()
    assert re.search(r"(?m)^trioditis results$", top), "the one command, on its own line"
    assert "(#help-measure)" in top and "default is No" in top
    assert not re.search(r"\d+(\.\d+)?\s*%|\b\d+/\d+\b", top.split("## See it decide", 1)[1].split("<picture>", 1)[0]), \
        "no figures from model runs next to the demo"


def test_the_demo_shows_lines_that_explain_really_prints(tmp_path, capsys, monkeypatch):
    """Every line of "See it decide" is the real output of `explain` on the example config with only an
    Anthropic key set, except the lines marked as left out."""
    from trioditis.__main__ import main
    block = first_screen().split("## See it decide", 1)[1]
    demo = re.search(r"```text\n(.*?)```", block, re.S).group(1)
    runs = re.split(r"(?m)^\$ trioditis explain ", demo)[1:]
    assert len(runs) == 2
    folder = tmp_path / "router"
    with pytest.raises(SystemExit):
        main(["init", "--dir", str(folder)])
    monkeypatch.chdir(folder)
    for var in ("OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "placeholder")
    for run in runs:
        prompt_line, *shown = run.rstrip("\n").split("\n")
        capsys.readouterr()
        main(["--config", str(folder / "config.yaml"), "explain", prompt_line.strip('"')])
        printed = capsys.readouterr().out.split("\n")
        kept = [line for line in shown if line.strip() != "…"]
        missing = [line for line in kept if line not in printed]
        assert missing == [], f"the README shows lines explain does not print: {missing}"
        order = [printed.index(line) for line in kept]
        assert order == sorted(order), "the README keeps the printed order"
