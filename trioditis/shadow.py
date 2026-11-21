"""Shadow counts: which route the router and the route picker would name for Claude Code's prompts.

Claude Code's requests pass through the gate (trioditis/gate.py) to Anthropic as sent; nothing routes
them. With `route_picker: {shadow_claude_code: true}`, the first main request of each Claude Code
session is also classified in the background, the same way a routed `auto` request is (router and
picker side by side, the picker's limits and timeout included), and one line of numbers is written to
`shadow.jsonl` beside the decision log. `trioditis eval-routes --log` counts those lines with the rest.

What it never does: change, hold or delay the request (the body is parsed in a thread after the
upstream call has started); send anything off this machine (the picker and the embeddings provider
must be local, config.py checks both); write prompt text (counts, routes, a session fingerprint).

Which request counts as the first turn: the first one in a session that carries `x-claude-code-session-id`,
no `x-claude-code-agent-id` (a subagent) and a non-empty `tools` list (Claude Code's side calls, such
as titles, send none). Text blocks that are a whole `<system-reminder>` are left out of the prompt:
they are Claude Code's context, not what the person typed. Both are this module's reading of what
Claude Code sends, to be checked against real sessions (docs/claude-code.md, "Shadow counts").
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


def prompt_of(body: dict) -> tuple[str, int, int]:
    """(the prompt to classify, characters of the whole last user message, reminder blocks left out).
    The prompt is the last user message's text blocks, minus blocks that are one whole system reminder."""
    for m in reversed(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            texts = [str(p.get("text", "")) for p in _parts(m.get("content"))
                     if isinstance(p, dict) and p.get("type") in ("text", "input_text")]
            kept = [t for t in texts if not _REMINDER.fullmatch(t)]
            return "\n".join(kept), len("\n".join(texts)), len(texts) - len(kept)
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
            text, message_chars, reminders = prompt_of(body)
            # Tools are left out on purpose: Claude Code always sends its own (Bash, Edit, ...), which
            # would match route code's tool keywords on every turn and skip the picker.
            _, router_route, info = await _classify_with_picker(cfg, text, self.embedder, self.picker, [], tools=None)
            model = body.get("model")
            rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "shadow": True, "source": "claude-code",
                   "router_route": router_route, "route_picker": info,
                   "prompt_chars": len(text), "message_chars": message_chars, "reminder_blocks": reminders,
                   "tools": len(body["tools"]) if isinstance(body["tools"], list) else None,
                   "model": model[:100] if isinstance(model, str) else None, "session_fp": fingerprint(sid)}
            self.append(self.path, json.dumps(rec), SHADOW_LOG_MAX)
        except Exception:  # noqa: S110 -- best-effort, like the gate's metering
            pass
