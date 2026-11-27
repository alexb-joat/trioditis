"""`trioditis migrate`: Trioditis's files leave the old shared folder for ~/.trioditis, a link stays at
each old path, Lunete's files stay put, nothing is merged, and an interrupted move finishes."""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from trioditis import migrate
from trioditis_metrics import home
from trioditis_metrics.db import MetricsDB

LINUX_PROC = Path("/proc/self/fdinfo").is_dir()
# Nothing moves on Windows yet (links there need extra rights): test_windows_keeps_the_old_folder covers it.
MOVES = pytest.mark.skipif(sys.platform == "win32", reason="nothing moves on Windows yet")


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """The layout on a machine that ran Hlidskjalv: ~/.lunete is the real folder, ~/.hlidskjalv links to it,
    and Trioditis's files sit there next to Lunete's."""
    monkeypatch.delenv("TRIODITIS_HOME", raising=False)
    monkeypatch.delenv("HLIDSKJALV_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(migrate, "WRITER_WAIT_S", 0)
    shared = tmp_path / ".lunete"
    shared.mkdir()
    (tmp_path / ".hlidskjalv").symlink_to(shared)
    db = MetricsDB(shared / "metrics.db")
    db.record({"route": "plain", "model": "m", "status": 200})
    db.close()
    (shared / "budget.json").write_text('{"daily_usd": 2}')
    (shared / "prices.json").write_text("{}")
    (shared / "prices.json.bak-2026-10-30").write_text("{}")
    (shared / "plan-salt").write_text("salt")
    (shared / "trials" / "2026-11-09").mkdir(parents=True)
    (shared / "trials" / "2026-11-09" / "run.jsonl").write_text("{}\n")
    (shared / "settings.json").write_text("{}")      # Lunete's
    (shared / "alerts.jsonl").write_text("")         # Lunete's
    return tmp_path


def _run(dry_run=False) -> int:
    return migrate.run(argparse.Namespace(dry_run=dry_run))


def _snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.is_symlink(), p.read_bytes() if p.is_file() else None)
            for p in sorted(folder.rglob("*"))}


@MOVES
def test_dry_run_lists_the_move_and_writes_nothing(machine, capsys):
    before = _snapshot(machine)
    assert _run(dry_run=True) == 0
    out = capsys.readouterr().out
    assert f"from {machine / '.lunete'} to {machine / '.trioditis'}" in out
    assert "metrics.db, budget.json, prices.json, plan-salt, trials, prices.json.bak-2026-10-30" in out
    assert "nothing written (dry run)" in out
    assert _snapshot(machine) == before


@MOVES
def test_move_leaves_links_and_lunete_files(machine, capsys):
    shared, new = machine / ".lunete", machine / ".trioditis"
    inode = (shared / "metrics.db").stat().st_ino
    assert _run() == 0
    for n in ("metrics.db", "budget.json", "prices.json", "prices.json.bak-2026-10-30", "plan-salt", "trials"):
        assert (new / n).exists() and not (new / n).is_symlink(), n
        assert (shared / n).is_symlink() and (shared / n).resolve() == new / n, n
    assert (new / "metrics.db").stat().st_ino == inode  # the same file, not a copy
    assert (new / "budget.json").read_text() == '{"daily_usd": 2}'
    assert (new / "trials" / "2026-11-09" / "run.jsonl").exists()
    for n in ("settings.json", "alerts.jsonl"):
        assert (shared / n).is_file() and not (shared / n).is_symlink() and not (new / n).exists()
    assert home.data_home() == new
    # Readers that still use the old paths see the same data.
    ro = sqlite3.connect(f"file:{machine / '.hlidskjalv' / 'metrics.db'}?mode=ro", uri=True)
    assert ro.execute("SELECT count(*) FROM calls").fetchone()[0] == 1
    ro.close()
    out = capsys.readouterr().out
    assert "edit budget.json and prices.json in" in out
    log = (new / "migrate.log").read_text().splitlines()
    assert len(log) == 6 and all("the old path is now a link" in ln for ln in log)
    assert not list(machine.glob(".*.migrating"))


@MOVES
def test_running_again_does_nothing(machine, capsys):
    assert _run() == 0
    after = _snapshot(machine)
    capsys.readouterr()
    assert _run() == 0
    assert "nothing to move: no Trioditis files left" in capsys.readouterr().out
    assert _snapshot(machine) == after


@MOVES
def test_both_folders_with_different_files_merge_nothing(machine, capsys):
    new = machine / ".trioditis"
    new.mkdir()
    (new / "budget.json").write_text('{"daily_usd": 9}')
    before = _snapshot(machine)
    assert _run() == 1
    out = capsys.readouterr().out
    assert "budget.json" in out and "nothing is merged" in out and "ln -sf" in out
    assert _snapshot(machine) == before
    assert home.data_home() == new  # the new folder wins
    assert migrate.check_lines() and "nothing is merged" in migrate.check_lines()[0]


@MOVES
def test_an_old_file_saved_over_its_link_is_reported(machine, capsys):
    assert _run() == 0
    shared = machine / ".lunete"
    (shared / "budget.json").unlink()
    (shared / "budget.json").write_text('{"daily_usd": 5}')  # what an editor's atomic save leaves
    lines = migrate.check_lines()
    assert len(lines) == 1 and "budget.json" in lines[0] and "nothing is merged" in lines[0]


@MOVES
def test_an_interrupted_move_finishes(machine):
    """Killed after the new folder appeared but before the old paths became links: same files in both."""
    shared, new = machine / ".lunete", machine / ".trioditis"
    new.mkdir()
    for n in ("metrics.db", "budget.json"):
        os.link(shared / n, new / n)
    p = migrate.plan()
    assert p.finish == ["metrics.db", "budget.json"] and not p.clash
    assert _run() == 0
    for n in ("metrics.db", "budget.json", "plan-salt", "trials"):
        assert (shared / n).is_symlink() and (shared / n).resolve() == new / n


@MOVES
def test_a_stale_staging_folder_is_cleared(machine):
    stage = machine / "..trioditis.migrating"
    stage.mkdir()
    (stage / "metrics.db").write_text("half a copy")
    assert _run() == 0
    assert not stage.exists()
    assert sqlite3.connect(machine / ".trioditis" / "metrics.db").execute("SELECT count(*) FROM calls").fetchone()[0] == 1


@pytest.mark.parametrize("env, note", [
    ({"TRIODITIS_HOME": "chosen"}, "TRIODITIS_HOME is set"),
    ({"HLIDSKJALV_HOME": "chosen"}, "rename the variable to TRIODITIS_HOME"),
])
def test_one_variable_alone_moves_nothing(machine, monkeypatch, env, note):
    for k, v in env.items():
        monkeypatch.setenv(k, str(machine / v))
    p = migrate.plan()
    assert not p.work and note in p.note
    assert migrate.check_lines() == []


@MOVES
def test_both_variables_move_between_them(machine, monkeypatch):
    """The Docker image's case: the old variable names the old folder in the volume."""
    monkeypatch.setenv("HLIDSKJALV_HOME", str(machine / ".lunete"))
    monkeypatch.setenv("TRIODITIS_HOME", str(machine / "volume" / "trioditis"))
    assert _run() == 0
    assert (machine / ".lunete" / "metrics.db").resolve() == machine / "volume" / "trioditis" / "metrics.db"


@MOVES
def test_links_still_work_when_the_volume_is_seen_from_another_root(machine, monkeypatch):
    """The image's volume is /app/logs inside and ./logs on the host: absolute links would dangle there."""
    volume = machine / "app" / "logs"
    (volume / "hlidskjalv").mkdir(parents=True)
    for n in ("metrics.db", "budget.json", "trials"):
        os.rename(machine / ".lunete" / n, volume / "hlidskjalv" / n)
    monkeypatch.setenv("HLIDSKJALV_HOME", str(volume / "hlidskjalv"))
    monkeypatch.setenv("TRIODITIS_HOME", str(volume / "trioditis"))
    assert _run() == 0
    assert os.readlink(volume / "hlidskjalv" / "metrics.db") == os.path.join("..", "trioditis", "metrics.db")
    host = machine / "host-logs"
    os.rename(volume, host)  # the same folder under another path, as the host sees it
    db = sqlite3.connect(f"file:{host / 'hlidskjalv' / 'metrics.db'}?mode=ro", uri=True)
    assert db.execute("select count(*) from calls").fetchone() == (1,)
    db.close()
    assert (host / "hlidskjalv" / "budget.json").read_text() == '{"daily_usd": 2}'
    assert (host / "hlidskjalv" / "trials" / "2026-11-09" / "run.jsonl").is_file()


def test_windows_keeps_the_old_folder(machine, monkeypatch, capsys):
    monkeypatch.setattr(migrate, "_windows", lambda: True)
    before = _snapshot(machine)
    assert _run() == 0
    assert "on Windows the files stay in" in capsys.readouterr().out
    assert _snapshot(machine) == before
    assert home.data_home() == machine / ".hlidskjalv"


def test_a_folder_with_only_lunete_files_is_not_trioditis_home(monkeypatch, tmp_path):
    """A fresh Trioditis next to an existing Lunete starts its own folder, never writes into Lunete's."""
    monkeypatch.delenv("TRIODITIS_HOME", raising=False)
    monkeypatch.delenv("HLIDSKJALV_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".hlidskjalv").mkdir()
    (tmp_path / ".hlidskjalv" / "settings.json").write_text("{}")
    assert home.data_home() == tmp_path / ".trioditis"
    assert migrate.plan().note.startswith("nothing to move")


@pytest.mark.skipif(not LINUX_PROC, reason="the writer check reads /proc (Linux)")
def test_a_running_writer_stops_the_move(machine, capsys):
    db = machine / ".lunete" / "metrics.db"
    holder = subprocess.Popen([sys.executable, "-c",
                               "import os,sys,time; fd=os.open(sys.argv[1], os.O_RDWR); print('open', flush=True); "
                               "time.sleep(30)", str(db)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "open"
        before = _snapshot(machine)
        assert _run() == 1
        err = capsys.readouterr().err
        assert f"pid {holder.pid}" in err and "systemctl --user stop trioditis" in err
        assert _snapshot(machine) == before
    finally:
        holder.kill()
        holder.wait()


@pytest.mark.skipif(not LINUX_PROC, reason="the writer check reads /proc (Linux)")
def test_a_reader_does_not_stop_the_move(machine):
    db = machine / ".lunete" / "metrics.db"
    holder = subprocess.Popen([sys.executable, "-c",
                               "import sqlite3,sys,time; c=sqlite3.connect(f'file:{sys.argv[1]}?mode=ro', uri=True); "
                               "c.execute('SELECT count(*) FROM calls').fetchone(); print('open', flush=True); "
                               "time.sleep(30)", str(db)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "open"
        assert migrate.writers(db) == []
        assert _run() == 0
    finally:
        holder.kill()
        holder.wait()


@MOVES
def test_a_crashed_writers_journal_is_rolled_back_before_the_move(machine):
    db = machine / ".lunete" / "metrics.db"
    writer = subprocess.Popen([sys.executable, "-c",
                               "import sqlite3,sys,time; c=sqlite3.connect(sys.argv[1], isolation_level=None); "
                               "c.execute('PRAGMA cache_size=1'); c.execute('BEGIN IMMEDIATE'); "
                               "c.executemany('INSERT INTO calls (ts, route, model, local, status) VALUES (0,?,?,0,200)', "
                               "[('x'*400, 'y'*400)]*3000); print('mid', flush=True); time.sleep(30)", str(db)],
                              stdout=subprocess.PIPE, text=True)
    assert writer.stdout.readline().strip() == "mid"
    writer.kill()
    writer.wait()
    assert Path(str(db) + "-journal").exists()  # a hot journal, and pages of the unfinished write in the file
    assert _run() == 0
    new = sqlite3.connect(machine / ".trioditis" / "metrics.db")
    assert new.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert new.execute("SELECT count(*) FROM calls").fetchone()[0] == 1  # the unfinished write is gone
    new.close()
    assert not Path(str(db) + "-journal").exists()


@MOVES
def test_writers_write_through_an_old_path_link(machine):
    """`prices import --prices ~/.hlidskjalv/prices.json` after the move updates the file, keeps the link."""
    from trioditis.pricefacts import write_prices
    assert _run() == 0
    old = machine / ".hlidskjalv" / "prices.json"
    write_prices(old, {"m": {"input": 1, "output": 2}})
    assert (machine / ".lunete" / "prices.json").is_symlink()
    assert '"input": 1' in (machine / ".trioditis" / "prices.json").read_text()


def test_serve_moves_the_folder_before_anything_opens_it(monkeypatch, tmp_path):
    """The modules that fix a default path at import time are not loaded before `serve` calls at_start."""
    code = ("import sys, trioditis.__main__, trioditis.config, trioditis.migrate; "
            "print([m for m in ('trioditis_metrics.db', 'trioditis_metrics.prices', 'trioditis_metrics.guard', "
            "'trioditis.app', 'trioditis.refit', 'trioditis.adopted', 'trioditis.plan', 'trioditis.hardware') "
            "if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=Path(__file__).resolve().parent.parent).stdout.strip()
    assert out == "[]"

    from trioditis import __main__ as cli
    calls = []
    monkeypatch.setattr(migrate, "at_start", lambda: calls.append("migrate"))
    monkeypatch.setattr("trioditis.app.create_app", lambda cfg: calls.append("app"))
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: calls.append("run"))
    monkeypatch.chdir(tmp_path)
    assert cli.init_folder(tmp_path) == 0
    cli.main(["--config", str(tmp_path / "config.yaml"), "serve"])
    assert calls == ["migrate", "app", "run"]
