"""Shadow counts: which route the router and the route picker would name for Claude Code's prompts.

Claude Code's requests pass through the gate (trioditis/gate.py) to Anthropic as sent; nothing routes
them. With `route_picker: {shadow_claude_code: true}`, the first main request of each Claude Code
session is also classified in the background, the same way a routed `auto` request is (router and
picker side by side, the picker's limits and timeout included), and one line of numbers is written to
`shadow.jsonl` beside the decision log. `trioditis eval-routes --log` counts those lines with the rest.

When the picker misses its timeout (`cold` or `timeout`, usually a model still loading), the line also
waits for the call to end and records its late answer under `route_picker.late` ({outcome, route, ms}).
The outcome stays what a served request would have had; `late` adds which route the picker would have
named, so its choices can be judged before anyone pays (memory, a smaller model) to make it faster.

What it never does: change, hold or delay the request (the body is parsed in a thread after the
upstream call has started); send anything off this machine (the picker and the embeddings provider
must be local, config.py checks both); write prompt text. The line holds counts, routes, the picker's
limits in force (route_picker.limits), a session fingerprint and `prompt_fp`, a 16-hex-character hash of
the prompt the picker was given (security.fingerprint, the decision log's formula). The hash cannot be read
back into text, but it can confirm a prompt someone already has: hashing that text gives the same value,
which is how a local script can join a shadow line to a prompt it already holds.

Which request counts as the first turn: the first one in a session that carries `x-claude-code-session-id`,
no `x-claude-code-agent-id` (a subagent), a non-empty `tools` list (Claude Code's side calls, such
as titles, send none) and no assistant message yet (so after a restart, a chat that is mid-task is
not counted again; its session is remembered without a line). Text blocks that are a whole
`<system-reminder>` are left out of the prompt, and so are Claude Code's own wrappers wherever they
sit (the notice and command lines after /clear or another local command, IDE selections, task
notifications): they are Claude Code's context, not what the person typed. Both are this module's
reading of what Claude Code sends, to be checked against real sessions (docs/claude-code.md,
"Shadow counts").
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .router import Embedder, RoutePickerClient, _classify_with_picker, _parts
from .security import fingerprint

SHADOW_LOG_MAX = 10 * 1024 * 1024   # one line per session: years of use before the first rotation
SEEN_MAX = 10_000                    # sessions remembered, oldest forgotten first
_REMINDER = re.compile(r"\s*<system-reminder>.*</system-reminder>\s*", re.S)
# Claude Code's wrappers: what a local command (/clear, /model, ...) printed, the IDE's selection and
# open file, a background task's notice. Removed by tag, as separate blocks or inside typed text.
_WRAPPERS = re.compile(r"<(local-command-caveat|local-command-stdout|local-command-stderr|command-name|"
                       r"command-message|command-args|ide_selection|ide_opened_file|task-notification)>"
                       r".*?</\1>", re.S)


def _unwrapped(text: str) -> str:
    """The text without Claude Code's wrappers; typed text without any stays exactly as sent."""
    out = _WRAPPERS.sub("", text)
    return out.strip() if out != text else text


def prompt_of(body: dict) -> tuple[str, int, int]:
    """(the prompt to classify, characters of the whole last user message, reminder blocks left out).
    The prompt is the last user message's text blocks, minus blocks that are one whole system reminder
    and minus Claude Code's wrappers (_WRAPPERS)."""
    for m in reversed(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            texts = [str(p.get("text", "")) for p in _parts(m.get("content"))
                     if isinstance(p, dict) and p.get("type") in ("text", "input_text")]
            kept = [t for t in texts if not _REMINDER.fullmatch(t)]
            reminders = len(texts) - len(kept)
            kept = [t for t in (_unwrapped(t) for t in kept) if t]
            return "\n".join(kept), len("\n".join(texts)), reminders
    return "", 0, 0


class Shadow:
    """Owns the shadow log and the sessions already counted. `offer` is called by the gate for every
    Messages request; it returns at once and never raises."""

    def __init__(self, decisions_path: str | None, current: Callable[[], Config], embedder: Embedder | None,
                 picker: RoutePickerClient | None, append: Callable[[Path, str, int], None]):
        self.path = Path(decisions_path).with_name("shadow.jsonl") if decisions_path else None
        self.current, self.embedder, self.picker, self.append = current, embedder, picker, append
        self.seen: OrderedDict[str, None] = OrderedDict()
        self.tasks: set[asyncio.Task] = set()

    def on(self, cfg: Config) -> bool:
        p = cfg.route_picker
        return bool(self.path and self.picker and p is not None and p.shadow_claude_code)

    def offer(self, headers, raw: bytes) -> None:
        try:
            cfg = self.current()
            sid = (headers.get("x-claude-code-session-id") or "")[:200]
            if not sid or headers.get("x-claude-code-agent-id") or sid in self.seen or not self.on(cfg):
                return
            task = asyncio.get_running_loop().create_task(self._run(cfg, sid, raw))
            self.tasks.add(task)  # held, so the task is not collected half-way
            task.add_done_callback(self.tasks.discard)
        except Exception:  # noqa: S110 -- a shadow count is optional; it must never touch the request
            pass

    async def _run(self, cfg: Config, sid: str, raw: bytes) -> None:
        try:
            body = await asyncio.to_thread(json.loads, raw)
            if not isinstance(body, dict) or not body.get("tools") or sid in self.seen:
                return  # a side call (no tools): the session's first turn is still to come
            self.seen[sid] = None  # no await since the check above: two requests can't both count
            while len(self.seen) > SEEN_MAX:
                self.seen.popitem(last=False)
            messages = body.get("messages") if isinstance(body.get("messages"), list) else []
            if any(isinstance(m, dict) and m.get("role") == "assistant" for m in messages):
                return  # mid-task (the router restarted since this session began): not a first turn
            text, message_chars, reminders = prompt_of(body)
            # Tools are left out on purpose: Claude Code always sends its own (Bash, Edit, ...), which
            # would match route code's tool keywords on every turn and skip the picker.
            _, router_route, info = await _classify_with_picker(cfg, text, self.embedder, self.picker, [], tools=None,
                                                                late=True)
            model = body.get("model")
            p = cfg.route_picker
            info["limits"] = {"timeout_ms": p.timeout_ms, "max_chars": p.max_chars,
                              "keep_alive_minutes": p.keep_alive_minutes}  # so counts can be split by setting
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "shadow": True, "source": "claude-code",
                   "router_route": router_route, "route_picker": info, "prompt_fp": fingerprint(text),
                   "prompt_chars": len(text), "message_chars": message_chars, "reminder_blocks": reminders,
                   "messages": len(messages),
                   "tools": len(body["tools"]) if isinstance(body["tools"], list) else None,
                   "model": model[:100] if isinstance(model, str) else None, "session_fp": fingerprint(sid)}
            self.append(self.path, json.dumps(rec), SHADOW_LOG_MAX)
        except Exception:  # noqa: S110 -- best-effort, like the gate's metering
            pass
