"""The workflows keep what the README and docs/security-model.md say about them: actions pinned to commit SHAs,
read-only workflows. A new write scope, a moved tag or an injectable `run:` line is a deliberate edit to this
file, not an accident. Each rule has a negative case on an in-memory copy of a real workflow, so each can fail."""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# The only write scopes that may appear, at job level: (file, job) -> scopes. Adding one is a decision.
ALLOWED_JOB_WRITES = {
    ("codeql.yml", "analyze"): {"security-events"},      # uploads results to the Security tab
    ("release.yml", "pypi"): {"id-token"},               # PyPI trusted publishing: no API token exists
    ("release.yml", "github-release"): {"contents"},     # attaches the files to the GitHub release
}

PINNED = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}")
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
UNTRUSTED = re.compile(r"(?<![\w.])(github\.event\.|inputs\.)")
FETCH_AND_RUN = re.compile(r"\b(pipx\s+(run|install)|uvx|uv\s+tool\s+(run|install)|npx)\b")
PIP_INSTALL = re.compile(r"\bpip3?\s+install\b([^\n|;&]*)")


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


FILES = sorted(WORKFLOWS.glob("*.yml"))
DOCS = {p.name: load(p) for p in FILES}


def jobs(wf: dict) -> dict[str, dict]:
    return wf.get("jobs") or {}


def steps(wf: dict):
    for job_id, job in jobs(wf).items():
        for step in job.get("steps") or []:
            yield job_id, step


# ---------------------------------------------------------------- the rules: each returns what breaks it

def unpinned_uses(wf: dict) -> list[str]:
    refs = [(job_id, job["uses"]) for job_id, job in jobs(wf).items() if "uses" in job]
    refs += [(job_id, step["uses"]) for job_id, step in steps(wf) if "uses" in step]
    return [f"{job_id}: {ref}" for job_id, ref in refs
            if not (isinstance(ref, str) and (ref.startswith("./") or PINNED.fullmatch(ref)))]


def write_scopes(perms) -> set[str]:
    """The scopes a `permissions:` value grants write access to (`write-all` counts as every one)."""
    if isinstance(perms, str):
        return {"*"} if perms.strip().lower() == "write-all" else set()
    return {scope for scope, level in (perms or {}).items() if str(level).strip().lower() == "write"}


def permission_problems(name: str, wf: dict) -> list[str]:
    out = []
    if "permissions" not in wf:                     # `permissions: {}` counts: it is present and grants nothing
        out.append("no top-level permissions:")
    elif write_scopes(wf["permissions"]):
        out.append(f"top-level write scope {sorted(write_scopes(wf['permissions']))}")
    for job_id, job in jobs(wf).items():
        extra = write_scopes(job.get("permissions")) - ALLOWED_JOB_WRITES.get((name, job_id), set())
        if extra:
            out.append(f"job {job_id}: write scope {sorted(extra)} is not in the test's list")
    return out


def pull_request_target(wf: dict) -> bool:
    triggers = wf.get("on", wf.get(True))           # YAML 1.1 reads a bare `on` as True
    names = [triggers] if isinstance(triggers, str) else list(triggers or [])
    return "pull_request_target" in names


def injectable_runs(wf: dict) -> list[str]:
    out = []
    for job_id, step in steps(wf):
        for expr in EXPRESSION.findall(str(step.get("run", ""))):
            if UNTRUSTED.search(expr):
                out.append(f"{job_id}: run: uses ${{{{{expr}}}}}")
    return out


def unpinned_tools(wf: dict) -> list[str]:
    """A `run:` line that fetches a tool at whatever version is newest that day (`pipx run`, `uvx`, `npx`), or a
    `pip install` that neither checks hashes nor is a local file installed with `--no-deps`. One hijacked release
    of such a tool would run in CI with the checkout in reach."""
    out = []
    for job_id, step in steps(wf):
        for line in str(step.get("run", "")).splitlines():
            if found := FETCH_AND_RUN.search(line):
                out.append(f"{job_id}: `{found.group(0)}` fetches the newest release: {line.strip()}")
            for args in PIP_INSTALL.findall(line):
                local_only = "--no-deps" in args and all(a.startswith("-") or "/" in a for a in args.split())
                if "--require-hashes" not in args and not local_only:
                    out.append(f"{job_id}: pip install without --require-hashes: {line.strip()}")
    return out


def unpinned_images(wf: dict) -> list[str]:
    out = []
    for job_id, job in jobs(wf).items():
        images = [job["container"]] if "container" in job else []
        images += [job["services"][s] for s in job.get("services") or {}]
        for entry in images:
            image = entry if isinstance(entry, str) else (entry or {}).get("image", "")
            if not re.search(r"@sha256:[0-9a-f]{64}$", str(image)):
                out.append(f"{job_id}: {image or entry}")
    return out


# ---------------------------------------------------------------- today's files obey them

def test_there_are_workflows_to_check_and_the_allowed_writes_still_name_real_jobs():
    assert len(FILES) >= 6
    for (name, job_id), scopes in ALLOWED_JOB_WRITES.items():
        assert write_scopes(jobs(DOCS[name])[job_id].get("permissions")) >= scopes, (name, job_id)


@pytest.mark.parametrize("name", DOCS)
def test_every_uses_is_a_local_path_or_a_full_commit_sha(name):
    assert unpinned_uses(DOCS[name]) == []


@pytest.mark.parametrize("name", DOCS)
def test_every_workflow_is_read_only_at_the_top_and_writes_only_where_listed(name):
    assert permission_problems(name, DOCS[name]) == []


@pytest.mark.parametrize("name", DOCS)
def test_no_workflow_runs_on_pull_request_target(name):
    assert not pull_request_target(DOCS[name])


@pytest.mark.parametrize("name", DOCS)
def test_no_run_line_holds_an_event_or_input_expression(name):
    assert injectable_runs(DOCS[name]) == []


@pytest.mark.parametrize("name", DOCS)
def test_every_tool_a_run_line_installs_is_hash_pinned(name):
    assert unpinned_tools(DOCS[name]) == []


@pytest.mark.parametrize("name", DOCS)
def test_every_service_and_container_image_is_pinned_by_digest(name):
    assert unpinned_images(DOCS[name]) == []


def test_the_rules_look_at_what_the_workflows_really_contain():
    """A rule that never sees an `on:`, a `uses:`, a service or a `run:` would pass for the wrong reason."""
    assert sum(len(list(steps(wf))) for wf in DOCS.values()) > 20
    assert sum("uses" in s for wf in DOCS.values() for _, s in steps(wf)) >= 10
    assert any(jobs(wf)[j].get("services") for wf in DOCS.values() for j in jobs(wf))
    assert all(wf.get("on", wf.get(True)) for wf in DOCS.values())
    assert sum(len(PIP_INSTALL.findall(str(s.get("run", "")))) for wf in DOCS.values() for _, s in steps(wf)) >= 5
    assert any(re.search(r"\$\{\{\s*inputs\.", str(v)) for wf in DOCS.values() for job in jobs(wf).values()
               for v in (job.get("env") or {}).values()), "an input read through env: is the form the rule allows"


# ---------------------------------------------------------------- negative cases: each rule can fail

def edited(name: str) -> dict:
    return copy.deepcopy(DOCS[name])


def first_step_with(wf: dict, key: str) -> dict:
    return next(s for _, s in steps(wf) if key in s)


def test_a_tag_a_short_sha_and_a_docker_reference_are_not_pinned_but_a_local_path_is():
    for bad in ("actions/checkout@v5", "actions/checkout@" + "a" * 39, "actions/checkout@" + "A" * 40,
                "docker://alpine:3.20", "actions/checkout"):
        wf = edited("ci.yml")
        first_step_with(wf, "uses")["uses"] = bad
        assert unpinned_uses(wf), bad
    wf = edited("ci.yml")
    first_step_with(wf, "uses")["uses"] = "./.github/actions/mine"
    assert unpinned_uses(wf) == []
    wf = edited("ci.yml")
    first_step_with(wf, "uses")["uses"] = "github/codeql-action/init@" + "0" * 40
    assert unpinned_uses(wf) == []
    wf = edited("ci.yml")
    jobs(wf)["test"]["uses"] = "octo/org/.github/workflows/x.yml@main"          # a reusable workflow on a branch
    assert unpinned_uses(wf)


def test_a_missing_or_writing_top_level_permissions_block_fails():
    wf = edited("ci.yml")
    del wf["permissions"]
    assert permission_problems("ci.yml", wf)
    for bad in ("write-all", {"contents": "write"}, {"contents": "read", "issues": "write"}):
        wf = edited("ci.yml")
        wf["permissions"] = bad
        assert permission_problems("ci.yml", wf), bad
    wf = edited("release.yml")                       # `permissions: {}` is a block that grants nothing
    assert wf["permissions"] == {} and permission_problems("release.yml", wf) == []


def test_a_new_job_level_write_scope_fails_and_a_listed_one_does_not():
    wf = edited("ci.yml")
    jobs(wf)["test"]["permissions"] = {"contents": "write"}
    assert permission_problems("ci.yml", wf)
    wf = edited("ci.yml")
    jobs(wf)["test"]["permissions"] = "write-all"
    assert permission_problems("ci.yml", wf)
    wf = edited("release.yml")
    jobs(wf)["build"]["permissions"] = {"contents": "read", "id-token": "write"}      # listed for `pypi`, not for `build`
    assert permission_problems("release.yml", wf)
    wf = edited("codeql.yml")
    jobs(wf)["analyze"]["permissions"]["packages"] = "write"
    assert permission_problems("codeql.yml", wf)


def test_pull_request_target_fails_in_every_form_of_on():
    for triggers in ({"pull_request_target": None}, ["push", "pull_request_target"], "pull_request_target"):
        wf = edited("ci.yml")
        wf.pop("on", None)
        wf.pop(True, None)
        wf["on"] = triggers
        assert pull_request_target(wf), triggers
    assert not pull_request_target(edited("ci.yml"))


def test_an_event_or_input_expression_in_a_run_string_fails_but_the_same_in_env_does_not():
    for expr in ("github.event.pull_request.title", "github.event.head_commit.message", "inputs.model", "inputs.repeat"):
        wf = edited("bench.yml")
        first_step_with(wf, "run")["run"] = f'echo "${{{{ {expr} }}}}"'
        assert injectable_runs(wf), expr
    wf = edited("bench.yml")
    first_step_with(wf, "run")["run"] = "echo ${{ matrix.os }} ${{ github.sha }} $MODEL"
    assert injectable_runs(wf) == []
    wf = edited("bench.yml")
    first_step_with(wf, "run")["env"] = {"TITLE": "${{ github.event.pull_request.title }}"}
    assert injectable_runs(wf) == []


def test_a_tool_fetched_at_its_newest_release_fails_but_a_hash_checked_or_local_install_does_not():
    for run in ("pipx run pip-audit -r requirements.txt --require-hashes --strict", "uvx ruff check .",
                "uv tool run ruff check .", "npx prettier --check .", "pip install pip-audit",
                "python -m pip install --no-deps build", "pip install -r requirements.txt",
                "pip install --require-hashes -r a.txt && pip install twine"):
        wf = edited("ci.yml")
        first_step_with(wf, "run")["run"] = run
        assert unpinned_tools(wf), run
    for run in ("pip install --require-hashes --no-deps -r requirements-dev.txt",
                "/tmp/clean/bin/pip install --no-deps dist/*.whl",
                "python -m venv /tmp/audit\n/tmp/audit/bin/pip install --require-hashes --no-deps -r requirements-audit.txt\n"
                "/tmp/audit/bin/pip-audit -r requirements.txt --require-hashes --strict"):
        wf = edited("ci.yml")
        first_step_with(wf, "run")["run"] = run
        assert unpinned_tools(wf) == [], run


def test_an_image_without_a_digest_fails_in_services_and_container():
    wf = edited("bench.yml")
    jobs(wf)["results"]["services"]["ollama"]["image"] = "ollama/ollama:0.40.0"
    assert unpinned_images(wf)
    wf = edited("bench.yml")
    jobs(wf)["results"]["services"]["ollama"]["image"] = "ollama/ollama:0.40.0@sha256:abc"
    assert unpinned_images(wf)
    for container in ("ubuntu:22.04", {"image": "ubuntu:22.04"}):
        wf = edited("ci.yml")
        jobs(wf)["test"]["container"] = container
        assert unpinned_images(wf), container
    wf = edited("ci.yml")
    jobs(wf)["test"]["container"] = {"image": "ubuntu:22.04@sha256:" + "0" * 64}
    assert unpinned_images(wf) == []


def test_pypi_classifiers_name_exactly_the_python_versions_and_systems_ci_tests():
    import tomllib

    from trioditis import __version__
    meta = tomllib.loads((WORKFLOWS.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    classifiers = meta["classifiers"]
    matrix = load(WORKFLOWS / "ci.yml")["jobs"]["test"]["strategy"]["matrix"]
    claimed = {c.rsplit(" :: ", 1)[1] for c in classifiers if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)}
    assert claimed == set(matrix["python"])
    systems = {"ubuntu": "Operating System :: POSIX :: Linux", "macos": "Operating System :: MacOS",
               "windows": "Operating System :: Microsoft :: Windows"}
    assert {c for c in classifiers if c.startswith("Operating System")} == \
        {systems[os.split("-")[0]] for os in matrix["os"]}
    stable = "Development Status :: 5 - Production/Stable" in classifiers
    assert stable == (not __version__.startswith("0.")), "Production/Stable comes with 1.0.0, and only then"
