"""Routing values the reinforcement loop adopted, kept apart from config.yaml (docs/staying-current.md).

The file (trioditis-adopted.json in the data folder, private) holds the values in force, every change with
its before and after, and what the current label batch has decided. It is used only while the config
has a `reinforce:` block: without the block the router never opens it. A file that cannot be read, or
that holds a value outside what the loop may set, is ignored whole: the config's values serve, and
`trioditis check` says why. Nothing here calls a network service.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path

from trioditis_metrics.home import data_home

from .config import Config

SCHEMA = "trioditis-adopted.v1"
THRESHOLD = "embeddings.threshold"
PICKER_SERVES = "route_picker.serves"
SETTINGS = (THRESHOLD, PICKER_SERVES)
# What the loop may set, and nothing else (plan limits 1 and 3): the threshold inside these clamps,
# one step at a time, and whether the picker's route is served.
THRESHOLD_MIN, THRESHOLD_MAX, THRESHOLD_STEP = 0.35, 0.55, 0.05
_EPS = 1e-9


class AdoptedError(Exception):
    """The adopted file can't be used; the message names the cause and what to do."""


def adopted_path() -> Path:
    return data_home() / "trioditis-adopted.json"


def empty() -> dict:
    return {"schema": SCHEMA, "values": {}, "history": []}


def now_iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def check_value(setting: str, value) -> str | None:
    """None when the loop may set `setting` to `value`, else why not."""
    if setting == THRESHOLD:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{THRESHOLD} must be a number (got {value!r})"
        if not THRESHOLD_MIN - _EPS <= value <= THRESHOLD_MAX + _EPS:
            return f"{THRESHOLD} {value:g} is outside {THRESHOLD_MIN:g} to {THRESHOLD_MAX:g}, the range the loop may set"
        return None
    if setting == PICKER_SERVES:
        return None if isinstance(value, bool) else f"{PICKER_SERVES} must be true or false (got {value!r})"
    return f"{setting!r} is not a value the loop may set (only {' and '.join(SETTINGS)})"


def read(path: Path | None = None) -> dict:
    """The adopted document; an empty one when there is no file yet."""
    path = path or adopted_path()
    fix = f"fix or delete {path}; while it is unusable the config.yaml values are in force"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty()
    except OSError as err:
        raise AdoptedError(f"cannot read the adopted values: {err.strerror or err}; {fix}") from None
    try:
        doc = json.loads(text)
    except ValueError:
        raise AdoptedError(f"the adopted values file is not JSON; {fix}") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise AdoptedError(f"the adopted values file is not a {SCHEMA} document; {fix}")
    values, history = doc.get("values"), doc.get("history")
    if not isinstance(values, dict) or not isinstance(history, list):
        raise AdoptedError(f"the adopted values file lacks its values or history; {fix}")
    for setting, held in values.items():
        why = check_value(setting, held.get("value") if isinstance(held, dict) else None)
        if why:
            raise AdoptedError(f"the adopted values file holds a value the loop may not set: {why}; {fix}")
    return doc


def write(doc: dict, path: Path | None = None) -> None:
    path = path or adopted_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, indent=1) + "\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- the layers

def config_value(cfg: Config, setting: str):
    """What config.yaml (or the built-in default) gives `setting`; None when the config has no such part."""
    if setting == THRESHOLD:
        return cfg.embeddings.threshold if cfg.embeddings else None
    if setting == PICKER_SERVES:
        return True if cfg.route_picker else None
    raise KeyError(setting)


def effective(cfg: Config, values: dict, setting: str):
    """The value in force for `setting`: the adopted one when the loop is on and holds one."""
    base = config_value(cfg, setting)
    if base is None or cfg.reinforce is None or setting not in values:
        return base
    return values[setting]["value"]


def apply(cfg: Config, doc: dict) -> Config:
    """`cfg` with the adopted values over it. `cfg` itself when the loop is off or nothing is adopted."""
    if cfg.reinforce is None or not doc.get("values"):
        return cfg
    emb, picker = cfg.embeddings, cfg.route_picker
    if emb is not None:
        emb = dataclasses.replace(emb, threshold=float(effective(cfg, doc["values"], THRESHOLD)))
    if picker is not None:
        picker = dataclasses.replace(picker, serves=bool(effective(cfg, doc["values"], PICKER_SERVES)))
    if emb == cfg.embeddings and picker == cfg.route_picker:
        return cfg
    return dataclasses.replace(cfg, embeddings=emb, route_picker=picker)


def layers(cfg: Config, doc: dict) -> list[dict]:
    """One row per setting the config has: its value, the layer that set it (default, config or
    adopted) and, for an adopted one, since when and what the config would give."""
    rows = []
    for setting in SETTINGS:
        base = config_value(cfg, setting)
        if base is None:
            continue
        below = "config" if setting == PICKER_SERVES or cfg.embeddings.threshold_given else "default"
        held = doc.get("values", {}).get(setting) if cfg.reinforce is not None else None
        if held is None:
            rows.append({"setting": setting, "value": base, "layer": below})
        else:
            rows.append({"setting": setting, "value": held["value"], "layer": "adopted", "at": held.get("at"),
                         "below": base, "below_layer": below})
    return rows


def show(value) -> str:
    return ("yes" if value else "no") if isinstance(value, bool) else f"{value:g}"


def layer_lines(cfg: Config, doc: dict) -> list[str]:
    out = []
    for r in layers(cfg, doc):
        name = "route picker serves" if r["setting"] == PICKER_SERVES else r["setting"]
        if r["layer"] == "adopted":
            out.append(f"{name} {show(r['value'])}: adopted {str(r.get('at') or '')[:10]} "
                       f"({'config.yaml' if r['below_layer'] == 'config' else 'the built-in default'} "
                       f"has {show(r['below'])})")
        else:
            out.append(f"{name} {show(r['value'])}: from "
                       f"{'config.yaml' if r['layer'] == 'config' else 'the built-in default'}")
    return out


# ---------------------------------------------------------------- changes (pure: the caller writes the file)

def _open_adoptions(history: list) -> list[dict]:
    """Adoptions not yet taken back, oldest first."""
    stack: list[dict] = []
    for e in history:
        if e.get("kind") == "adopt":
            stack.append(e)
        elif e.get("kind") == "revert" and stack:
            if e.get("all"):
                stack.clear()
            else:
                stack.pop()
    return stack


def adopt(cfg: Config, doc: dict, setting: str, value, *, at: str, gate: dict | None, reason: str) -> dict:
    """A new document with `setting` set to `value`, and the change in its history."""
    why = check_value(setting, value)
    if why:
        raise AdoptedError(why)
    before = effective(cfg, doc["values"], setting)
    if before is None:
        raise AdoptedError(f"the config has nothing for {setting} to change")
    if setting == THRESHOLD and abs(value - before) > THRESHOLD_STEP + _EPS:
        raise AdoptedError(f"{THRESHOLD} moves at most {THRESHOLD_STEP:g} a step ({before:g} to {value:g} asked)")
    if value == before:
        raise AdoptedError(f"{setting} is already {show(value)}")
    new = json.loads(json.dumps(doc))
    new["history"].append({"at": at, "kind": "adopt", "setting": setting, "before": before, "after": value,
                           "values_before": doc["values"], "gate": gate, "reason": reason})
    new["values"][setting] = {"value": value, "at": at}
    return new


def revert(cfg: Config, doc: dict, *, at: str, everything: bool = False, reason: str) -> tuple[dict, list[dict]]:
    """(new document, the changes taken back). The last adoption, or with `everything` all of them:
    the values return to what they were before it, and a value taken back is remembered so the same
    label batch does not bring it in again. ([] when nothing is adopted.)"""
    opened = _open_adoptions(doc["history"])
    if not opened:
        return doc, []
    new = json.loads(json.dumps(doc))
    undone = list(reversed(opened)) if everything else [opened[-1]]
    new["values"] = {} if everything else undone[0]["values_before"]
    batch = (doc.get("batch") or {}).get("sha256")
    entries = []
    for e in undone:
        entries.append({"at": at, "kind": "revert", "setting": e["setting"], "before": e["after"],
                        "after": (new["values"].get(e["setting"]) or {}).get("value", config_value(cfg, e["setting"])),
                        "undoes": e["at"], "reason": reason})
        new.setdefault("reverted", []).append({"setting": e["setting"], "value": e["after"], "batch": batch})
    if everything:
        for e in entries:
            e["all"] = True  # closes every open adoption (see _open_adoptions)
    new["history"].extend(entries)
    return new, entries


def last_change_day(doc: dict) -> str:
    """The local date (YYYY-MM-DD) of the newest change, or ""."""
    return str(doc["history"][-1].get("at", ""))[:10] if doc.get("history") else ""


# ---------------------------------------------------------------- the running router's view

class Layer:
    """The config with the adopted values over it, for the running router: the file is looked at again
    at most once a second and re-read when it has changed, so an adoption or a revert needs no restart.
    An unusable file leaves the config's own values in force (`error` says why)."""

    def __init__(self, cfg: Config, path: Path | None = None, every_s: float = 1.0):
        self.cfg = cfg
        self.path = path or adopted_path()
        self.every_s = every_s
        self.error: str | None = None
        self._seen: tuple | None = ()
        self._checked = float("-inf")
        self._live = cfg

    def current(self) -> Config:
        if self.cfg.reinforce is None:
            return self.cfg
        now = time.monotonic()
        if now - self._checked < self.every_s:
            return self._live
        self._checked = now
        try:
            st = self.path.stat()
            seen = (st.st_mtime_ns, st.st_size)
        except OSError:
            seen = None
        if seen == self._seen:
            return self._live
        self._seen = seen
        try:
            self._live, self.error = apply(self.cfg, read(self.path)), None
        except AdoptedError as err:
            self._live, self.error = self.cfg, str(err)
        return self._live
