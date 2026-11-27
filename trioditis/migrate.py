"""`trioditis migrate`: the one-time move of Trioditis's files out of the old shared
folder (~/.hlidskjalv, which Lunete also uses) into its own (~/.trioditis).

Which folders:
  neither variable set              ~/.hlidskjalv     -> ~/.trioditis
  HLIDSKJALV_HOME and TRIODITIS_HOME $HLIDSKJALV_HOME  -> $TRIODITIS_HOME
  only one of them                  nothing moves: that folder is your choice

How: only the files Trioditis owns move (home.OWNED_*); everything else in the old
folder stays. Each file is hard-linked (or, across disks, copied) into a staging folder
that is renamed into place in one step, and then each old path is swapped for a link
to the new file in one step, so a dashboard reading the old path never sees a gap.
metrics.db is held with an exclusive SQLite lock throughout, which also rolls back a
journal left by a crash. It is safe to run again: a finished move is not repeated, an
interrupted one is completed, and when both folders hold the same file under different
contents nothing is merged (Trioditis uses the new folder; the old file is left alone).

On Windows, where links need extra rights, nothing moves yet: Trioditis keeps reading
the old folder. Every move is printed and appended to migrate.log in the new folder.
"""

from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from trioditis_metrics.home import OWNED_DIRS, owned_names

WRITER_RETRIES = 3       # a writer seen on metrics.db is asked about this many times...
WRITER_WAIT_S = 0.5      # ...this far apart, before the move is refused
LOCK_TIMEOUT_S = 5.0     # how long to wait for the exclusive lock on metrics.db
EDITED_BY_HAND = ("budget.json", "prices.json")


@dataclass
class Plan:
    source: Path | None = None        # the old folder, through its links
    target: Path | None = None        # the new folder
    move: list[str] = field(default_factory=list)     # entries to move now
    finish: list[str] = field(default_factory=list)   # already in target (same file): only the link is missing
    clash: list[str] = field(default_factory=list)    # in both folders as different files
    note: str = ""                    # why nothing moves, when nothing does

    @property
    def work(self) -> bool:
        return bool(self.move or self.finish) and not self.clash


def _windows() -> bool:
    return sys.platform == "win32"


def folders() -> tuple[Path | None, Path | None, str]:
    """(old, new, note): which folders the move is between, from the variables the user set."""
    new, old = os.environ.get("TRIODITIS_HOME"), os.environ.get("HLIDSKJALV_HOME")
    if new and old:
        return Path(old).expanduser(), Path(new).expanduser(), ""
    if not new and not old:
        return Path.home() / ".hlidskjalv", Path.home() / ".trioditis", ""
    if new:
        return None, None, f"TRIODITIS_HOME is set ({new}): nothing to move"
    return None, None, (f"HLIDSKJALV_HOME is set ({old}), so Trioditis keeps using that folder. To move, set "
                        f"TRIODITIS_HOME to the new folder as well and run `trioditis migrate`; or just rename "
                        f"the variable to TRIODITIS_HOME and keep the folder")


def plan() -> Plan:
    old, new, note = folders()
    if old is None or new is None:
        return Plan(note=note)
    src = old.resolve()
    if not src.is_dir():
        return Plan(note=f"nothing to move: {old} does not exist")
    names = [n for n in owned_names(src) if not (src / n).is_symlink()]
    if not names:
        return Plan(note=f"nothing to move: no Trioditis files left in {src}")
    dst = new.resolve()
    if dst == src:
        return Plan(note=f"nothing to move: {old} and {new} are the same folder")
    if _windows():
        return Plan(source=src, target=dst, note=(
            f"on Windows the files stay in {src} for now (links there need extra rights); "
            f"Trioditis keeps reading them"))
    p = Plan(source=src, target=dst)
    for n in names:
        there = dst / n
        if not (there.exists() or there.is_symlink()):
            p.move.append(n)
        elif n not in OWNED_DIRS and there.exists() and os.path.samefile(src / n, there):
            p.finish.append(n)
        else:
            p.clash.append(n)
    if p.clash:
        p.note = (f"both {src} and {dst} have {', '.join(p.clash)}: Trioditis uses {dst} and nothing is merged. "
                  f"If you edited the old copy (an editor that saves by replacing the file cuts the link), "
                  f"copy the change into {dst}, then replace the old file with a link, for example: "
                  f"ln -sf {dst / p.clash[0]} {src / p.clash[0]}")
    return p


def writers(db: Path) -> list[str]:
    """Other processes holding `db` open for writing, as "pid N (name)". Linux only (reads /proc);
    elsewhere [] and the exclusive lock is the only guard. Read-only openers (dashboards) don't count."""
    proc = Path("/proc")
    if not (proc / "self" / "fdinfo").is_dir():
        return []
    real, me, found = str(db.resolve()), os.getpid(), []
    for pdir in proc.iterdir():
        if not pdir.name.isdigit() or int(pdir.name) == me:
            continue
        try:
            fds = list((pdir / "fd").iterdir())
        except OSError:  # another user's process, or gone
            continue
        for fd in fds:
            try:
                if os.readlink(fd) != real:
                    continue
                info = (pdir / "fdinfo" / fd.name).read_text()
                flags = int(next(ln for ln in info.splitlines() if ln.startswith("flags:")).split()[1], 8)
            except (OSError, StopIteration, ValueError, IndexError):
                continue
            if flags & os.O_ACCMODE != os.O_RDONLY:
                try:
                    name = (pdir / "comm").read_text().strip()
                except OSError:
                    name = "?"
                found.append(f"pid {pdir.name} ({name})")
                break
    return found


class Refused(Exception):
    """The move cannot start safely right now; nothing was changed."""


def _lock(db: Path) -> sqlite3.Connection:
    for attempt in range(WRITER_RETRIES):
        busy = writers(db)
        if not busy:
            break
        if attempt == WRITER_RETRIES - 1:
            raise Refused(f"{db} is open for writing by {', '.join(busy)}: stop it "
                          f"(for the service: systemctl --user stop trioditis), then run `trioditis migrate`")
        time.sleep(WRITER_WAIT_S)
    if Path(str(db) + "-wal").exists():
        raise Refused(f"{db} has a write-ahead log ({db}-wal) that this move would leave behind; "
                      f"open the database once with sqlite3 and run PRAGMA journal_mode=DELETE, then retry")
    conn = sqlite3.connect(db, timeout=LOCK_TIMEOUT_S, isolation_level=None)
    try:
        conn.execute("BEGIN EXCLUSIVE")  # rolls back a journal left by a crashed writer, then holds everyone off
    except sqlite3.OperationalError as err:
        conn.close()
        raise Refused(f"could not lock {db} ({err}): another program is writing to it; retry in a moment") from err
    return conn


def apply(p: Plan, log) -> list[str]:
    """Carry out the plan. Returns the moved names. Raises Refused before changing anything."""
    if not (p.work and p.source and p.target):
        raise ValueError("apply() needs a plan with work in it")
    src, dst = p.source, p.target
    lock = _lock(src / "metrics.db") if "metrics.db" in p.move + p.finish else None
    try:
        files = [n for n in p.move if n not in OWNED_DIRS]
        dirs = [n for n in p.move if n in OWNED_DIRS]
        copied = set()
        if files:
            stage = dst.parent / f".{dst.name}.migrating"
            if stage.exists():
                shutil.rmtree(stage)  # left by an interrupted run: it only ever holds links or copies
            dst.parent.mkdir(parents=True, exist_ok=True)
            stage.mkdir(mode=0o700)
            for n in files:
                try:
                    os.link(src / n, stage / n)
                except OSError:  # another disk, or a file system without hard links
                    shutil.copy2(src / n, stage / n)
                    copied.add(n)
            if dst.exists():
                for n in files:
                    os.rename(stage / n, dst / n)
                stage.rmdir()
            else:
                os.rename(stage, dst)  # the new folder appears whole, never half-filled
        dst.mkdir(mode=0o700, parents=True, exist_ok=True)
        for n in files + p.finish:
            tmp = src / f".{n}.trioditis-link"
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            os.symlink(_link_to(dst / n, src), tmp)
            os.replace(tmp, src / n)  # the old path turns into a link in one step
            how = "copied (other disk)" if n in copied else "moved"
            log(f"{how} {src / n} -> {dst / n}; the old path is now a link")
        for n in dirs:
            try:
                os.rename(src / n, dst / n)
                how = "moved"
            except OSError as err:
                if err.errno != errno.EXDEV:
                    raise
                shutil.copytree(src / n, dst / n, symlinks=True)
                shutil.rmtree(src / n)
                how = "copied (other disk)"
            os.symlink(_link_to(dst / n, src), src / n, target_is_directory=True)
            log(f"{how} {src / n}/ -> {dst / n}/; the old path is now a link")
    finally:
        if lock is not None:
            lock.execute("ROLLBACK")
            lock.close()
    return p.move + p.finish


def _link_to(target: Path, folder: Path) -> str:
    """The link text for `target`, relative to `folder` (for example ../.trioditis/metrics.db), so the
    link also works where the two folders sit under another root: a Docker volume seen from the host."""
    try:
        return os.path.relpath(target, folder)
    except ValueError:  # another drive on Windows: no relative path exists
        return str(target)


def _logger(p: Plan, out):
    def log(line: str) -> None:
        print(line, file=out)
        try:
            fd = os.open(p.target / "migrate.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}\n")
        except OSError:
            pass  # the line is printed either way
    return log


def run(args) -> int:
    """The `migrate` command. 0: moved, or nothing to move; 1: refused, or both folders hold the files."""
    p = plan()
    if not p.move and not p.finish:
        print(p.note)
        return 1 if p.clash else 0
    if p.clash:
        print(p.note)
        return 1
    names = ", ".join(p.move + p.finish)
    if args.dry_run:
        print(f"would move from {p.source} to {p.target}, leaving a link at each old path: {names}")
        print(f"everything else in {p.source} stays there")
        busy = writers(p.source / "metrics.db") if "metrics.db" in p.move + p.finish else []
        if busy:
            print(f"metrics.db is open for writing by {', '.join(busy)}: stop it before the real run")
        print("nothing written (dry run)")
        return 0
    try:
        moved = apply(p, _logger(p, sys.stdout))
    except Refused as err:
        print(f"nothing moved: {err}", file=sys.stderr)
        return 1
    _after(moved, p, sys.stdout)
    return 0


def at_start() -> None:
    """Called by `serve` before anything reads the data folder. Never stops the router: if the move
    can't happen now, Trioditis keeps using the old folder and says why on stderr."""
    try:
        p = plan()
    except OSError as err:
        print(f"warning: data folder check failed ({err}); `trioditis migrate --dry-run` shows the state",
              file=sys.stderr)
        return
    if p.clash or (p.note and p.source):  # both folders hold the files, or Windows
        print(f"warning: {p.note}", file=sys.stderr)
    if not p.work:
        return
    try:
        moved = apply(p, _logger(p, sys.stderr))
    except (Refused, OSError) as err:
        print(f"warning: the data folder was not moved ({err}); Trioditis keeps using {p.source}",
              file=sys.stderr)
        return
    _after(moved, p, sys.stderr)


def _after(moved: list[str], p: Plan, out) -> None:
    print(f"Trioditis's data folder is now {p.target}", file=out)
    hand = [n for n in EDITED_BY_HAND if n in moved]
    if hand:
        print(f"edit {' and '.join(hand)} in {p.target} from now on: an editor that saves by replacing "
              f"the file would cut the link at the old path", file=out)


def check_lines() -> list[str]:
    """For `check`: Trioditis files still waiting in the old folder, or in both folders."""
    try:
        p = plan()
    except OSError as err:
        return [f"data folder: could not inspect it ({err})"]
    if p.clash:
        return [f"data folder: {p.note}"]
    if p.work:
        return [f"data folder: Trioditis files are still in {p.source} ({', '.join(p.move + p.finish)}); "
                f"`trioditis migrate --dry-run` shows the move to {p.target}, and `serve` makes it when it starts"]
    if p.note and p.source:  # Windows
        return [f"data folder: {p.note}"]
    return []
