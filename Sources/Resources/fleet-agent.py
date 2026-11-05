#!/usr/bin/env python3
"""TokNotch Fleet agent: reports what agents are doing on this machine.

TokNotch pipes this file into `python3 -I -u - watch` over ssh, so nothing is
installed on the remote machine and the agent always matches the app. It ends
by itself once the ssh connection goes away, because its next write fails.

`watch` and `--once` are read-only. They report token counts per day and model
(see docs/FLEET-USAGE.md), model names, tool names, the file name a worker last
touched, a short title per session or worker, the OS account name and, from
`~/.claude.json`, the Claude account's email, plan name and organization name.
Nothing else from that file is reported. They never report message bodies,
commands or other tool inputs, or anything from credential files: those are read
only to tell whether a key is set.

`cancel RUN_ID` stops one running mm-worker task and prints one JSON line.

`rules-set BASE64 REVISION` and `rules-reset REVISION` change this machine's
dispatch rules (see docs/FLEET-RULES.md) and print one JSON line.

Standard library only, Python 3.9 or newer.

Output is one JSON object per line. `--once` prints a single snapshot.
"""

import base64
import ctypes
import fcntl
import glob
import http.client
import json
import os
import platform
import pwd
import re
import select
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

AGENT_VERSION = 5
SCAN_INTERVAL = 2.0  # how often known session files are checked for growth
WALK_INTERVAL = 15.0  # how often the tree is walked for new session files
HEARTBEAT = 5.0  # longest gap between snapshots, so CPU and temperature stay current
TEMPERATURE_INTERVAL = 10.0  # temperature changes slowly and costs more to read
DISPATCHER_INTERVAL = 10.0  # rules, keys and the installed dispatcher change rarely
SLOT_GRACE = 5.0  # mm-worker gives a new slot this long to get its pid
LOCK_TIMEOUT = 10.0  # longest wait for another rules writer to finish
RULES_LIMIT = 1 << 16  # real rules are well under 2 KB
SESSION_WINDOW = 6 * 3600  # sessions quieter than this are left out of the list
MAX_SESSIONS = 60
FINISHED_JOB_WINDOW = 3600  # how long a finished worker stays listed
JOB_WINDOW = 7 * 86400  # older job folders are ignored without being read
TITLE_LENGTH = 60
READ_CHUNK = 1 << 20
HISTORY_DAYS = 29  # past days reported in usageHistory, today not included
HISTORY_PASS_BYTES = 16 << 20  # most the past-days scan reads in one pass of the main loop
HISTORY_INTERVAL = 15.0  # least time between two usageHistory sent while the first scan runs
ACCOUNT_INTERVAL = 60.0  # how often ~/.claude.json is checked
ACCOUNT_LIMIT = 32 << 20  # a bigger ~/.claude.json is not read

# Where Linux publishes CPU and sensor data. Tests point these at a fixture.
PROC_STAT = "/proc/stat"
SYS_CLASS = "/sys/class"
# Sensor drivers that measure the CPU itself, best first.
LINUX_CPU_SENSORS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "cpu-thermal", "soc_thermal")
LINUX_CPU_ZONES = ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc-thermal", "soc_thermal", "cpu", "acpitz")
LINUX_CPU_LABELS = ("tctl", "tdie", "package id 0")

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
CLAUDE_CONFIG = os.path.join(HOME, ".claude.json")
WORKER_STATE = os.path.join(HOME, ".cache", "mm-worker")
DISPATCHER = os.path.join(HOME, ".local", "bin", "mm-worker")
RULES_DIR = os.path.join(HOME, ".config", "mm-worker")
RULES_PATH = os.path.join(RULES_DIR, "rules.json")
RULES_LOCK = os.path.join(RULES_DIR, ".rules.lock")
# ssh without a login shell has a thin PATH, so these are checked as well.
CLAUDE_DIRS = (os.path.join(HOME, ".local", "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")

PROVIDERS = ("minimax", "zhipu", "claude", "qwen")
API_KEYS = {"minimax": "MINIMAX_API_KEY", "zhipu": "ZHIPU_API_KEY", "qwen": "QWEN_API_KEY"}
MODELS = {
    "minimax": ("m3", "m27", "fast"),
    "zhipu": ("flash",),
    "claude": ("sonnet", "haiku", "claude-opus-5-5"),
    "qwen": ("glm", "max", "flash", "lite", "ds"),
}
LEVELS = ("simple", "normal", "hard")

# Besides the short names above, a rule may name a model as the provider's own
# API does. The prefix keeps one provider's model off another, and keeps a key
# pasted into a rules file from passing as a model.
MODEL_PREFIXES = {
    "minimax": ("minimax-",),
    "zhipu": ("glm-",),
    "claude": ("claude-",),
    "qwen": ("qwen", "glm-", "deepseek-"),
}
FULL_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")


def is_full_model_name(provider, model):
    return (isinstance(model, str) and FULL_MODEL_NAME.fullmatch(model) is not None
            and model.lower().startswith(MODEL_PREFIXES[provider]))


# The first mm-worker that accepts full model names in its rules. An older one
# would call the file invalid and silently go back to its defaults.
FULL_NAMES_DISPATCHER = 2
# The first mm-worker that reads the rule "cli", for the same reason.
CLI_DISPATCHER = 3
CLIS = ("claude", "opencode")
OPENCODE_DIRS = (os.path.join(HOME, ".local", "bin"), os.path.join(HOME, ".opencode", "bin"),
                 "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin")

# Where each provider lists its models: its host variable, and the path below it.
# Claude isn't here, as a subscription has no key this could ask with.
MODEL_LISTS = {
    "minimax": ("MINIMAX_API_HOST", "/anthropic/v1/models"),
    "zhipu": ("ZHIPU_API_HOST", "/v1/models"),
    "qwen": ("QWEN_API_HOST", "/v1/models"),
}
MODEL_LIST_TIMEOUT = 10
MODEL_LIST_PAGES = 5

# Kept equal to the copy in mm-worker.py and to docs/FLEET-RULES.md.
DEFAULT_RULES = {
    "version": 1,
    "revision": 0,
    "balance": "proportional",
    "order": ["minimax", "zhipu", "claude"],
    "providers": {
        "minimax": {"enabled": True, "slots": 8},
        "zhipu": {"enabled": True, "slots": 4},
        "claude": {"enabled": True, "slots": 4},
        "qwen": {"enabled": False, "slots": 4},
    },
    "levels": {
        "simple": {"minimax": "fast", "zhipu": "flash", "claude": "sonnet"},
        "normal": {"minimax": "m3", "zhipu": "flash", "claude": "sonnet"},
        "hard": {"claude": "claude-opus-5-5"},
    },
    "defaults": {"timeoutSeconds": 900, "cooldownSeconds": 1800},
}


def local_midnight(now):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def parse_timestamp(text):
    """Epoch seconds from the `2026-11-01T07:25:36.123Z` form, or None."""
    if not isinstance(text, str) or len(text) < 19:
        return None
    try:
        parsed = time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    # calendar.timegm without the import cost of calendar's locale tables.
    days = _days_from_civil(parsed.tm_year, parsed.tm_mon, parsed.tm_mday)
    return days * 86400 + parsed.tm_hour * 3600 + parsed.tm_min * 60 + parsed.tm_sec


def _days_from_civil(year, month, day):
    year -= month <= 2
    era = year // 400
    yoe = year - era * 400
    doy = (153 * (month + (-3 if month > 2 else 9)) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def tidy_title(text):
    """One readable line of at most TITLE_LENGTH characters."""
    if not text:
        return ""
    lines = (re.sub(r"^[#>*\-\s]+", "", line).strip() for line in text.replace("�", "").splitlines())
    lines = [line for line in lines if line]
    # Worker prompts often open with boilerplate; the line naming the task says more.
    for line in lines:
        if re.match(r"(任务|task)\s*[:：]", line, re.IGNORECASE):
            return line[:TITLE_LENGTH]
    return lines[0][:TITLE_LENGTH] if lines else ""


class SessionLog:
    """Reads Claude Code session files incrementally."""

    def __init__(self):
        self.files = {}  # path -> {"offset", "mtime", "session"}
        # Streaming writes one assistant message to several lines, and a resumed
        # session repeats earlier lines in a new file, so counting is per
        # message id, keeping the latest usage seen.
        self.messages = {}  # message id -> record
        self.sessions = {}  # session id -> metadata
        self.usage_by_session = {}  # filled by report()
        self.streams = WorkerStreams()
        self.last_walk = 0.0

    def walk(self, midnight):
        """Find session files touched today."""
        for folder, _, names in os.walk(PROJECTS):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(folder, name)
                if path in self.files:
                    continue
                try:
                    if os.stat(path).st_mtime < midnight:
                        continue
                except OSError:
                    continue
                self.files[path] = {"offset": 0, "mtime": 0.0, "session": name[:-6]}

    def scan(self, now):
        midnight = local_midnight(now)
        if now - self.last_walk >= WALK_INTERVAL:
            self.last_walk = now
            self.walk(midnight)
        for path, state in list(self.files.items()):
            try:
                stat = os.stat(path)
            except OSError:
                del self.files[path]
                continue
            if stat.st_size < state["offset"]:
                state["offset"] = 0  # rewritten; read it again
            if stat.st_size > state["offset"]:
                self._read(path, state)
            state["mtime"] = stat.st_mtime
            session = self.sessions.get(state["session"])
            if session is not None:
                session["activity"] = max(session.get("activity", 0.0), stat.st_mtime)
        self._forget_before(midnight)

    def _read(self, path, state):
        try:
            with open(path, "rb") as handle:
                handle.seek(state["offset"])
                pending = b""
                while True:
                    chunk = handle.read(READ_CHUNK)
                    if not chunk:
                        break
                    pending += chunk
                    lines = pending.split(b"\n")
                    pending = lines.pop()
                    for line in lines:
                        state["offset"] += len(line) + 1
                        self._line(line, state)
        except OSError:
            pass

    def _line(self, line, state):
        # Most lines are neither; skipping them unparsed keeps a large file cheap.
        if b'"usage"' not in line and b'"ai-title"' not in line:
            return
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict):
            return
        session_id = entry.get("sessionId") or state["session"]
        state["session"] = session_id
        kind = entry.get("type")
        if kind == "ai-title":
            self._session(session_id)["title"] = tidy_title(entry.get("aiTitle"))
            return
        if kind != "assistant":
            return
        message = entry.get("message")
        if not isinstance(message, dict):
            return
        session = self._session(session_id)
        when = parse_timestamp(entry.get("timestamp"))
        if entry.get("cwd") and not entry.get("isSidechain"):
            session["cwd"] = entry["cwd"]
        if entry.get("entrypoint"):
            session["entrypoint"] = entry["entrypoint"]
        model = message.get("model")
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                    session["tool"] = block["name"]
        usage = message.get("usage")
        ident = message.get("id")
        if not isinstance(usage, dict) or not ident or model == "<synthetic>":
            return
        if model:
            session["model"] = model
        known = self.messages.get(ident)
        self.messages[ident] = {
            "session": known["session"] if known else session_id,
            "model": model or "unknown",
            "when": _earliest(known["when"] if known else None, when),
            "input": _count(usage, "input_tokens"),
            "output": _count(usage, "output_tokens"),
            "cacheRead": _count(usage, "cache_read_input_tokens"),
            "cacheWrite": _count(usage, "cache_creation_input_tokens"),
        }

    def _session(self, session_id):
        return self.sessions.setdefault(session_id, {})

    def _forget_before(self, midnight):
        """Across midnight, drop what can no longer appear in today's report."""
        for path, state in list(self.files.items()):
            if state["mtime"] and state["mtime"] < midnight:
                del self.files[path]

    def report(self, now, history=None):
        """Sessions and today's tokens by model. With a history, a message it has
        seen on an earlier day is that day's, so it is never counted twice."""
        midnight = local_midnight(now)
        today = local_day(now)
        by_session = {}
        by_model = {}
        for ident, record in self.messages.items():
            bucket = by_session.setdefault(record["session"], _empty_usage())
            _add(bucket, record)
            if (record["when"] is not None and record["when"] >= midnight
                    and (history is None or not history.earlier(ident, today))):
                _add(by_model.setdefault(record["model"], _empty_usage()), record)
        listed = []
        for ident, meta in self.sessions.items():
            activity = meta.get("activity", 0.0)
            if now - activity > SESSION_WINDOW or ident not in by_session:
                continue
            listed.append({
                "id": ident,
                "title": meta.get("title", ""),
                "kind": "worker" if meta.get("entrypoint") == "sdk-cli" else "interactive",
                "model": meta.get("model", ""),
                "cwd": _home_relative(meta.get("cwd", "")),
                "lastTool": meta.get("tool", ""),
                "idleSeconds": max(0, int(now - activity)),
                "usage": by_session[ident],
            })
        self.usage_by_session = by_session
        listed.sort(key=lambda item: item["idleSeconds"])
        return listed[:MAX_SESSIONS], by_model


def _earliest(first, second):
    if first is None:
        return second
    return first if second is None else min(first, second)


def local_day(epoch):
    """The local date as 2026-11-01. Interned, as many records share each day."""
    return sys.intern(time.strftime("%Y-%m-%d", time.localtime(epoch)))


def _days_back(now, days, hour):
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday - days, hour, 0, 0, 0, 0, -1))


class UsageHistory:
    """Tokens per local day and model for the HISTORY_DAYS days before today.

    Separate from SessionLog, which reads only today's files and must stay
    quick. The first scan reads every file touched within the window, at most
    HISTORY_PASS_BYTES per step, so a long log never holds up a live snapshot.
    Later steps read only what files gained.

    Today's messages are kept too, so that at midnight they move into the past
    days without a rescan. Days before yesterday keep only totals and the ids
    of their messages, enough to ignore a repeat: a message still being
    streamed can only be from yesterday or today.
    """

    def __init__(self):
        self.files = {}  # path -> {"offset", "pending", "mtime"}
        self.queue = []  # paths with unread bytes, oldest file first
        self.detail = {}  # message id -> (day, model, input, output, cacheRead, cacheWrite)
        self.totals = {}  # day -> model -> [input, output, cacheRead, cacheWrite], for compacted days
        self.seen = {}  # day -> ids of its messages, for compacted days
        self.walked = False
        self.last_walk = 0.0
        self.complete = False
        self.total_bytes = 0  # what the first scan has to read
        self.read_bytes = 0
        self.today = None
        self.changed = True
        self.days = None
        self.sent = None
        self.sent_at = None

    def _set_day(self, now):
        today = local_day(now)
        if today == self.today:
            return
        self.today = today
        self.start = local_day(_days_back(now, HISTORY_DAYS, 12))
        self.start_time = _days_back(now, HISTORY_DAYS, 0)
        self.yesterday = local_day(_days_back(now, 1, 12))
        for day in [day for day in self.totals if day < self.start]:
            del self.totals[day]
            self.seen.pop(day, None)
        for path in [path for path, state in self.files.items() if state["mtime"] < self.start_time]:
            del self.files[path]
        self.queue = [path for path in self.queue if path in self.files]
        self._compact()
        self.changed = True

    def _compact(self):
        """Fold the detail of days before yesterday into totals, once the
        first scan has seen every copy of those messages."""
        if not self.complete:
            return
        for ident, record in list(self.detail.items()):
            if record[0] < self.yesterday:
                del self.detail[ident]
                if record[0] >= self.start:
                    self._fold(ident, record)

    def _fold(self, ident, record):
        _add_counts(self.totals, record[0], record[1], record[2:])
        self.seen.setdefault(record[0], set()).add(ident)

    def _compacted(self, ident):
        return any(ident in ids for ids in self.seen.values())

    def earlier(self, ident, today):
        """Whether this message belongs to a day before today."""
        record = self.detail.get(ident)
        if record is not None:
            return record[0] < today
        return self._compacted(ident)

    def _walk(self, now):
        self.last_walk = now
        found = []
        for folder, _, names in os.walk(PROJECTS):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(folder, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_mtime < self.start_time:
                    continue  # nothing in it can be in the window, so it is not opened
                state = self.files.setdefault(path, {"offset": 0, "pending": b"", "mtime": 0.0})
                state["mtime"] = stat.st_mtime
                if stat.st_size < state["offset"]:
                    state.update(offset=0, pending=b"")  # rewritten; repeats count once
                if stat.st_size > state["offset"]:
                    found.append((stat.st_mtime, path, stat.st_size - state["offset"]))
        # Older files first, so the copy of a message in a resumed session is read last.
        found.sort()
        queued = set(self.queue)
        self.queue.extend(path for _, path, _ in found if path not in queued)
        if not self.walked:
            self.walked = True
            self.total_bytes = sum(size for _, _, size in found)

    def step(self, now, budget=None):
        """One pass of the main loop: read at most budget bytes, all there is if 0."""
        self._set_day(now)
        if budget is None:
            budget = HISTORY_PASS_BYTES
        if not self.queue and (not self.walked or now - self.last_walk >= WALK_INTERVAL):
            self._walk(now)
        left = budget or None
        while self.queue and (left is None or left > 0):
            used, finished = self._read(self.files[self.queue[0]], self.queue[0], left)
            if left is not None:
                left -= used
            if finished:
                self.queue.pop(0)
        if not self.complete and self.walked and not self.queue:
            self.complete = True
            self.changed = True
            self._compact()

    def finish(self, now):
        """Read everything there is, as --once does."""
        while not self.complete:
            self.step(now, 0)

    def _read(self, state, path, limit):
        """(bytes read, whether the file is done)."""
        used = 0
        try:
            with open(path, "rb") as handle:
                handle.seek(state["offset"])
                while limit is None or used < limit:
                    chunk = handle.read(READ_CHUNK if limit is None else min(READ_CHUNK, limit - used))
                    if not chunk:
                        return used, True
                    used += len(chunk)
                    state["offset"] += len(chunk)
                    if not self.complete:
                        self.read_bytes += len(chunk)
                    lines = (state["pending"] + chunk).split(b"\n")
                    state["pending"] = lines.pop()
                    for line in lines:
                        self._line(line)
        except OSError:
            return used, True
        return used, False

    def _line(self, line):
        if b'"usage"' not in line:
            return
        try:
            entry = json.loads(line)
        except ValueError:
            return
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            return
        message = entry.get("message")
        if not isinstance(message, dict):
            return
        usage = message.get("usage")
        ident = message.get("id")
        model = message.get("model")
        when = parse_timestamp(entry.get("timestamp"))
        if not isinstance(usage, dict) or not ident or model == "<synthetic>" or when is None:
            return
        day = local_day(when)
        known = self.detail.get(ident)
        if known is not None:
            day = min(day, known[0])
        elif self._compacted(ident):
            return  # counted already, and finished long ago
        elif day < self.start:
            return  # outside the window
        record = (day, sys.intern(model if isinstance(model, str) and model else "unknown"),
                  _count(usage, "input_tokens"), _count(usage, "output_tokens"),
                  _count(usage, "cache_read_input_tokens"), _count(usage, "cache_creation_input_tokens"))
        if record == known:
            return
        if known is None and self.complete and day < self.yesterday:
            self._fold(ident, record)
        else:
            self.detail[ident] = record
        if day < self.today:
            self.changed = True

    def report(self, now):
        """The usageHistory field, whether or not it is news."""
        self._set_day(now)
        if self.changed or self.days is None:
            self.changed = False
            days = {}
            for day, models in self.totals.items():
                for model, counts in models.items():
                    _add_counts(days, day, model, counts)
            for record in self.detail.values():
                if self.start <= record[0] < self.today:
                    _add_counts(days, record[0], record[1], record[2:])
            self.days = {}
            for day in sorted(days):
                models = {model: dict(zip(("input", "output", "cacheRead", "cacheWrite"), counts))
                          for model, counts in sorted(days[day].items()) if any(counts)}
                if models:
                    self.days[day] = models
        if self.complete:
            progress = 1.0
        else:
            progress = min(1.0, self.read_bytes / self.total_bytes) if self.total_bytes else 0.0
        return {"complete": self.complete, "progress": round(progress, 3),
                "days": json.loads(json.dumps(self.days))}  # a copy, so callers can't change the cache

    def news(self, now):
        """The usageHistory field if it changed since it was last sent, else None.
        While the first scan runs, at most one every HISTORY_INTERVAL."""
        if not self.complete and self.sent_at is not None and now - self.sent_at < HISTORY_INTERVAL:
            return None
        report = self.report(now)
        body = json.dumps(report, sort_keys=True)
        if body == self.sent:
            return None
        self.sent, self.sent_at = body, now
        return report


def _add_counts(days, day, model, counts):
    bucket = days.setdefault(day, {}).setdefault(model, [0, 0, 0, 0])
    for index in range(4):
        bucket[index] += counts[index]


def _count(usage, key):
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _empty_usage():
    return {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}


def _add(bucket, record):
    for key in bucket:
        bucket[key] += record[key]


def _home_relative(path):
    if path == HOME or path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


def _read_text(path, limit=4096):
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return None


def _alive(pid_text):
    try:
        pid = int((pid_text or "").strip())
    except ValueError:
        return False
    if pid <= 0:
        return False  # kill(0) and kill(-n) reach whole process groups
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by somebody else
    return True


def _worker_result(path):
    """(tokens, session id) from a finished worker's final line. Never its result text."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - (1 << 20)))
            lines = handle.read().splitlines()
        result = json.loads(lines[-1])
        usage = result.get("usage") or {}
    except (OSError, ValueError, IndexError, AttributeError):
        return None, None
    return {
        "input": _count(usage, "input_tokens"),
        "output": _count(usage, "output_tokens"),
        "cacheRead": _count(usage, "cache_read_input_tokens"),
        "cacheWrite": _count(usage, "cache_creation_input_tokens"),
    }, result.get("session_id")


class WorkerStreams:
    """Follows the event stream of each running worker.

    A stream's own usage figures are placeholders until the final result, so
    tokens come from the session log through the session id found here.
    """

    def __init__(self):
        self.states = {}

    def progress(self, path, now):
        """What a worker is doing, from its stream file. Empty if there is none."""
        state = self.states.setdefault(path, {"offset": 0, "messages": set(), "pending": b""})
        try:
            stat = os.stat(path)
        except OSError:
            return {}
        if stat.st_size < state["offset"]:
            state.update(offset=0, pending=b"", messages=set())
        if stat.st_size > state["offset"]:
            self._read(path, state)
        report = {"idleSeconds": max(0, int(now - stat.st_mtime))}
        if state["messages"]:
            report["steps"] = len(state["messages"])
        for key in ("sessionId", "model", "lastTool", "lastFile"):
            if state.get(key):
                report[key] = state[key]
        return report

    def _opencode_line(self, event, state):
        if event.get("sessionID"):
            state["sessionId"] = event["sessionID"]
        part = event.get("part")
        if not isinstance(part, dict):
            return
        if event["type"] == "step_finish":
            if part.get("messageID"):
                state["messages"].add(part["messageID"])
        elif part.get("tool"):
            state["lastTool"] = part["tool"]
            status = part.get("state") if isinstance(part.get("state"), dict) else {}
            given = status.get("input") if isinstance(status.get("input"), dict) else {}
            target = given.get("filePath")
            # The name only: enough to see what it is working on.
            state["lastFile"] = os.path.basename(target) if isinstance(target, str) else ""

    def forget_except(self, paths):
        for path in list(self.states):
            if path not in paths:
                del self.states[path]

    def _read(self, path, state):
        try:
            with open(path, "rb") as handle:
                handle.seek(state["offset"])
                while True:
                    chunk = handle.read(READ_CHUNK)
                    if not chunk:
                        return
                    state["offset"] += len(chunk)
                    lines = (state["pending"] + chunk).split(b"\n")
                    state["pending"] = lines.pop()
                    for line in lines:
                        self._line(line, state)
        except OSError:
            pass

    def _line(self, line, state):
        # Tool results make up most of a stream, and they can be huge.
        head = line[:200]
        if (b'"assistant"' not in head and b'"init"' not in head and b'"tool_use"' not in head
                and b'"step_finish"' not in head):
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("session_id"):
            state["sessionId"] = event["session_id"]
        if event.get("type") in ("tool_use", "step_finish"):
            self._opencode_line(event, state)
            return
        if event.get("type") == "system" and event.get("model"):
            state["model"] = event["model"]
        if event.get("type") != "assistant":
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        if message.get("id"):
            state["messages"].add(message["id"])
        if message.get("model") and message["model"] != "<synthetic>":
            state["model"] = message["model"]
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                state["lastTool"] = block["name"]
                target = (block.get("input") or {}).get("file_path")
                # The name only: enough to see what it is working on.
                state["lastFile"] = os.path.basename(target) if isinstance(target, str) else ""


def _add_progress(worker, stream, streams, usage_by_session, now):
    if streams is None:
        return
    worker.update(streams.progress(stream, now))
    usage = usage_by_session.get(worker.get("sessionId"))
    if usage:
        worker["usage"] = usage


def _task_title(name_path, prompt_path):
    """The name given with -n, or failing that a guess from the prompt."""
    return tidy_title(_read_text(name_path, 512)) or tidy_title(_read_text(prompt_path))


def remote_jobs(now, streams=None, usage_by_session=None):
    """Workers this machine runs for a dispatcher elsewhere (mm-remote-run)."""
    root = os.path.join(WORKER_STATE, "jobs")
    try:
        names = os.listdir(root)
    except OSError:
        return []
    jobs = []
    for name in names:
        folder = os.path.join(root, name)
        try:
            started = os.stat(os.path.join(folder, "started")).st_mtime
        except OSError:
            continue
        if now - started > JOB_WINDOW:
            continue
        code = _read_text(os.path.join(folder, "rc"))
        finished = None
        if code is None:
            state = "running" if _alive(_read_text(os.path.join(folder, "pid"))) else "lost"
        else:
            try:
                finished = os.stat(os.path.join(folder, "rc")).st_mtime
            except OSError:
                continue
            if now - finished > FINISHED_JOB_WINDOW:
                continue
            state = _finished_state(code.strip())
        if state == "lost" and now - started > FINISHED_JOB_WINDOW:
            continue
        run_id, _, provider = name.rpartition("-")
        job = {
            "id": run_id or name,
            "provider": provider,
            "state": state,
            "title": _task_title(os.path.join(folder, "name"), os.path.join(folder, "prompt.md")),
            "elapsedSeconds": int((finished or now) - started),
        }
        if finished is not None:
            usage, session = _worker_result(os.path.join(folder, "out.json"))
            if usage:
                job["usage"] = usage
            if session:
                job["sessionId"] = session
        elif state == "running":
            stream = os.path.join(folder, "out.json")
            _add_progress(job, stream, streams, usage_by_session or {}, now)
            job["stream"] = stream  # only so snapshot() knows what is still live
        jobs.append(job)
    return jobs


def _finished_state(code):
    if code == "0":
        return "done"
    if code == "cancelled":
        return "cancelled"
    if code in ("124", "137"):
        return "timeout"
    return "failed"


def local_workers(now, streams=None, usage_by_session=None):
    """Workers mm-worker holds a slot for on this machine, and any it sent away."""
    root = os.path.join(WORKER_STATE, "slots")
    runs = os.path.join(WORKER_STATE, "runs")
    workers = []
    try:
        providers = os.listdir(root)
    except OSError:
        providers = []
    for provider in providers:
        try:
            slots = os.listdir(os.path.join(root, provider))
        except OSError:
            continue
        for slot in slots:
            pid_path = os.path.join(root, provider, slot, "pid")
            pid = (_read_text(pid_path) or "").strip()
            if not pid or not _alive(pid):
                continue
            # A job run for a dispatcher elsewhere holds this slot too: remote_jobs lists it.
            if os.path.exists(os.path.join(root, provider, slot, "job")):
                continue
            run_id = _run_id_for(runs, pid)
            try:
                started = os.stat(pid_path).st_mtime
            except OSError:
                continue
            worker = {
                "id": run_id or pid,
                "provider": provider,
                "state": "running",
                "title": _task_title(os.path.join(runs, str(run_id) + ".name"),
                                     os.path.join(runs, str(run_id) + ".prompt.md")),
                "elapsedSeconds": int(now - started),
            }
            remote = _read_text(os.path.join(runs, str(run_id) + ".remote")) if run_id else None
            if remote and remote.split():
                worker["remoteHost"] = remote.split()[0]
            elif run_id:
                stream = os.path.join(runs, "%s.%s.json" % (run_id, provider))
                _add_progress(worker, stream, streams, usage_by_session or {}, now)
                worker["stream"] = stream
            workers.append(worker)
    return workers + _sent_workers(runs, now, {worker["id"] for worker in workers})


def _sent_workers(runs, now, seen):
    """Tasks a live dispatcher here is watching on another machine: they hold no slot here."""
    workers = []
    try:
        names = os.listdir(runs)
    except OSError:
        return workers
    for name in names:
        if not name.endswith(".remote"):
            continue
        run_id = name[: -len(".remote")]
        if run_id in seen or not _alive(run_id.rsplit("-", 1)[-1]):
            continue
        path = os.path.join(runs, name)
        record = (_read_text(path) or "").split()
        try:
            started = os.stat(path).st_mtime
        except OSError:
            continue
        # A pid gets reused in the end, and the record of a finished run stays.
        if len(record) < 3 or now - started > JOB_WINDOW:
            continue
        workers.append({
            "id": run_id,
            "provider": record[2],
            "state": "running",
            "title": _task_title(os.path.join(runs, run_id + ".name"), os.path.join(runs, run_id + ".prompt.md")),
            "elapsedSeconds": int(now - started),
            "remoteHost": record[0],
        })
    return workers


def _run_id_for(runs, pid):
    """A run id ends in the dispatcher's pid, and the newest match is the live one."""
    suffix = "-" + pid + ".prompt.md"
    try:
        matches = [name for name in os.listdir(runs) if name.endswith(suffix)]
    except OSError:
        return None
    return max(matches)[: -len(".prompt.md")] if matches else None


RUN_ID = re.compile(r"^[0-9]+-[0-9]+-[0-9]+$")


def _descendants(pid):
    """Every process below pid, children first found first."""
    found, queue = [], [str(pid)]
    while queue:
        parent = queue.pop()
        try:
            output = subprocess.run(["pgrep", "-P", parent], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        children = output.decode().split()
        found.extend(children)
        queue.extend(children)
    return found


def _signal(pid):
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _cancel_remote_job(folder):
    """A job started by mm-remote-run. Writing rc first tells the dispatcher that
    this was a cancellation, so it does not hand the task to another provider."""
    pid = (_read_text(os.path.join(folder, "pid")) or "").strip()
    if not pid.isdigit() or not _alive(pid):
        return {"ok": False, "error": "not running"}
    try:
        with open(os.path.join(folder, "rc"), "x") as handle:
            handle.write("cancelled\n")
    except FileExistsError:
        return {"ok": False, "error": "already finished"}
    except OSError as error:
        return {"ok": False, "error": str(error)}
    # `timeout` in mm-remote-run moves into its own process group, so this goes
    # by session: the job was started with setsid.
    subprocess.run(["pkill", "-TERM", "-s", pid], stderr=subprocess.DEVNULL, timeout=10)
    try:
        open(os.path.join(folder, "out.json"), "w").close()  # the stream can be large
    except OSError:
        pass
    return {"ok": True}


def _cancel_local_worker(run_id):
    """A worker running under mm-worker on this machine."""
    runs = os.path.join(WORKER_STATE, "runs")
    pid = run_id.rsplit("-", 1)[1]
    if os.path.exists(os.path.join(runs, run_id + ".remote")) and _alive(pid):
        return {"ok": False, "error": "runs on another machine"}
    slots = glob.glob(os.path.join(WORKER_STATE, "slots", "*", "*", "pid"))
    if not any((_read_text(path) or "").strip() == pid for path in slots) or not _alive(pid):
        return {"ok": False, "error": "not running"}
    try:
        command = subprocess.run(["ps", "-o", "command=", "-p", pid], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=5).stdout.decode()
    except (OSError, subprocess.SubprocessError):
        command = ""
    if "mm-worker" not in command:  # a recycled pid is somebody else's process
        return {"ok": False, "error": "not a worker"}
    # The marker first: killing the model makes the run look like a provider
    # failure, and mm-worker would otherwise retry it elsewhere.
    open(os.path.join(runs, run_id + ".cancelled"), "w").close()
    below = _descendants(pid)
    _signal(pid)
    for child in below:
        _signal(child)
    return {"ok": True}


def cancel(run_id):
    if not RUN_ID.match(run_id or ""):
        return {"ok": False, "error": "bad run id"}
    folders = glob.glob(os.path.join(WORKER_STATE, "jobs", run_id + "-*"))
    return _cancel_remote_job(folders[0]) if folders else _cancel_local_worker(run_id)


def _whole(value):
    # True and False are ints in Python, but not in the rules.
    return isinstance(value, int) and not isinstance(value, bool)


def _quote(name):
    return json.dumps(name[:40])


def _key_error(entry, field, expected):
    unknown = sorted(set(entry) - set(expected))
    if unknown:
        return "unknown key %s in %s" % (_quote(unknown[0]), field)
    for key in expected:
        if key not in entry:
            return "%s is missing from %s" % (key, field)
    return None


def rules_error(rules):
    """What is wrong with these rules, in plain English, or None.

    Messages name the field but never repeat its value, in case somebody
    pasted a key where a model name belongs.
    """
    if not isinstance(rules, dict):
        return "the rules must be a JSON object"
    # cli is optional, and absent means claude, so the defaults stay as they were.
    required = {key: value for key, value in rules.items() if key != "cli"}
    error = _key_error(required, "the rules", ("version", "revision", "balance", "order", "providers", "levels", "defaults"))
    if error:
        return error
    if rules.get("cli", "claude") not in CLIS:
        return "cli must be one of " + ", ".join(CLIS)
    if not _whole(rules["version"]) or rules["version"] != 1:
        return "version must be 1"
    if not _whole(rules["revision"]) or rules["revision"] < 0:
        return "revision must be a whole number, 0 or more"
    if rules["balance"] not in ("proportional", "priority"):
        return 'balance must be "proportional" or "priority"'
    providers = rules["providers"]
    if not isinstance(providers, dict) or not providers:
        return "providers must be an object naming at least one provider"
    for name in sorted(providers):
        field = "providers." + name
        if name not in MODELS:
            return "unknown provider %s in providers" % _quote(name)
        entry = providers[name]
        if not isinstance(entry, dict):
            return field + " must be an object"
        error = _key_error(entry, field, ("enabled", "slots"))
        if error:
            return error
        if not isinstance(entry["enabled"], bool):
            return field + ".enabled must be true or false"
        if not _whole(entry["slots"]) or not 1 <= entry["slots"] <= 32:
            return field + ".slots must be a whole number from 1 to 32"
    # The defaults leave the disabled qwen out of order, so a disabled provider
    # may be missing from it. Anything else must match providers exactly.
    order = rules["order"]
    enabled = [name for name in providers if providers[name]["enabled"]]
    if (not isinstance(order, list) or not all(isinstance(name, str) for name in order)
            or len(set(order)) != len(order) or not set(order) <= set(providers)
            or not set(enabled) <= set(order)):
        return "order must list each enabled provider in providers once, and nothing else"
    levels = rules["levels"]
    if not isinstance(levels, dict):
        return "levels must be an object"
    error = _key_error(levels, "levels", LEVELS)
    if error:
        return error
    for level in LEVELS:
        field = "levels." + level
        choice = levels[level]
        if not isinstance(choice, dict) or not choice:
            return field + " must name at least one provider"
        for name in sorted(choice):
            if name not in providers:
                return "%s names %s, which is not in providers" % (field, _quote(name))
            if choice[name] not in MODELS[name] and not is_full_model_name(name, choice[name]):
                return "%s.%s must be one of %s, or a %s model's full name" % (
                    field, name, ", ".join(MODELS[name]), name)
    defaults = rules["defaults"]
    if not isinstance(defaults, dict):
        return "defaults must be an object"
    error = _key_error(defaults, "defaults", ("timeoutSeconds", "cooldownSeconds"))
    if error:
        return error
    if not _whole(defaults["timeoutSeconds"]) or not 30 <= defaults["timeoutSeconds"] <= 86400:
        return "defaults.timeoutSeconds must be a whole number from 30 to 86400"
    if not _whole(defaults["cooldownSeconds"]) or not 0 <= defaults["cooldownSeconds"] <= 86400:
        return "defaults.cooldownSeconds must be a whole number from 0 to 86400"
    return None


def default_rules(revision=0):
    rules = json.loads(json.dumps(DEFAULT_RULES))  # a deep copy
    rules["revision"] = revision
    return rules


def load_rules():
    """(rules, valid). A missing file means the defaults. So does an unusable
    one, flagged invalid so the app can offer to repair it."""
    try:
        with open(RULES_PATH, "rb") as handle:
            data = handle.read(RULES_LIMIT + 1)
    except FileNotFoundError:
        return default_rules(), True
    except OSError:
        return default_rules(), False
    try:
        rules = json.loads(data.decode("utf-8")) if len(data) <= RULES_LIMIT else None
    except (ValueError, RecursionError):
        rules = None
    if rules is None or rules_error(rules):
        return default_rules(), False
    return rules, True


def _shell_word(text):
    """The value of `KEY=<text>` as a shell would read it, without running it.
    Expansions like $X stay literal: enough to tell empty from set."""
    word, quote, index = [], None, 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                quote = None
            else:
                word.append(char)
        elif quote == '"':
            if char == '"':
                quote = None
            elif char == "\\" and text[index + 1:index + 2] in ('"', "\\", "$", "`"):
                index += 1
                word.append(text[index])
            else:
                word.append(char)
        elif char in "'\"":
            quote = char
        elif char == "\\" and index + 1 < len(text):
            index += 1
            word.append(text[index])
        elif char in " \t;&|":
            break
        else:
            word.append(char)
        index += 1
    return "" if quote else "".join(word)  # an open quote would fail to source


ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _key_value(provider):
    """The provider's API key, or "". Callers test it, hide it or send it to the
    provider's own host: it is never stored, reported or logged."""
    return _env_value(provider, API_KEYS.get(provider))


def _env_value(provider, variable):
    text = _read_text(os.path.join(HOME, ".config", provider, "env"), 1 << 16) if variable else None
    value = ""
    for line in (text or "").lstrip("\ufeff").splitlines():
        match = ASSIGNMENT.match(line.strip())
        if match and match.group(1) == variable:
            value = _shell_word(match.group(2)).strip()  # the last one wins, as in a shell
    return value


def claude_installed():
    """Whether a `claude` command exists. It is never run."""
    for folder in os.environ.get("PATH", "").split(os.pathsep) + list(CLAUDE_DIRS):
        path = os.path.join(folder, "claude")
        # A relative PATH entry would depend on where ssh started us.
        if os.path.isabs(folder) and os.path.isfile(path) and os.access(path, os.X_OK):
            return True
    return False


def opencode_installed():
    """Whether an `opencode` command exists. It is never run."""
    for folder in os.environ.get("PATH", "").split(os.pathsep) + list(OPENCODE_DIRS):
        path = os.path.join(folder, "opencode")
        if os.path.isabs(folder) and os.path.isfile(path) and os.access(path, os.X_OK):
            return True
    return False


VERSION_LINE = re.compile(r"^MM_WORKER_VERSION\s*=\s*([0-9]+)\s*(?:#.*)?$", re.MULTILINE)


def dispatcher_version():
    """(installed, version) from the top of the installed file, without running
    it. The older shell dispatcher has no version line: version 0."""
    if not os.path.isfile(DISPATCHER):
        return False, 0
    match = VERSION_LINE.search(_read_text(DISPATCHER, 4096) or "")
    return True, (int(match.group(1)) if match else 0)


def used_slots(provider, now):
    """Live slots, counted the way mm-worker counts them."""
    root = os.path.join(WORKER_STATE, "slots", provider)
    try:
        slots = os.listdir(root)
    except OSError:
        return 0
    used = 0
    for slot in slots:
        folder = os.path.join(root, slot)
        pid = (_read_text(os.path.join(folder, "pid"), 64) or "").strip()
        if pid.isdigit():
            used += _alive(pid)
            continue
        try:
            used += now - os.stat(folder).st_mtime < SLOT_GRACE  # still waiting for its pid
        except OSError:
            pass
    return used


def _dispatcher_files():
    """Everything about the dispatcher that only changes when a file does."""
    installed, version = dispatcher_version()
    rules, valid = load_rules()
    claude = claude_installed()
    providers = {}
    for provider in PROVIDERS:
        key = _key_value(provider)
        try:
            until = float((_read_text(os.path.join(WORKER_STATE, provider + "_unavailable_until"), 64) or "").strip())
        except ValueError:
            until = 0.0
        reason = " ".join((_read_text(os.path.join(WORKER_STATE, provider + "_unavailable_reason"), 4096) or "").split())
        if len(key) >= 4:
            reason = reason.replace(key, "[hidden]")  # a provider's error may quote the key back
        providers[provider] = {
            "configured": claude if provider == "claude" else bool(key),
            "until": until,
            "reason": reason[:200],
        }
    return {"installed": installed, "version": version, "claudeInstalled": claude,
            "rules": rules, "rulesValid": valid, "providers": providers}


class Dispatcher:
    """mm-worker's state for snapshots. Files are re-read every
    DISPATCHER_INTERVAL; slots are only a directory listing, so they are counted
    on every pass."""

    def __init__(self):
        self.files = None
        self.files_at = None

    def read(self, now):
        if self.files_at is None or now - self.files_at >= DISPATCHER_INTERVAL:
            self.files_at = now
            self.files = _dispatcher_files()
        report = {key: value for key, value in self.files.items() if key != "providers"}
        report["providers"] = {}
        for provider, state in self.files["providers"].items():
            cooling = state["until"] > now
            report["providers"][provider] = {
                "configured": state["configured"],
                "used": used_slots(provider, now),
                "cooling": cooling,
                "coolingReason": state["reason"] if cooling else "",
            }
        return report


REVISION = re.compile(r"^[0-9]{1,15}$")
RULES_USAGE = "usage: rules-set BASE64 REVISION, or rules-reset REVISION"


def _why(error):
    return error.strerror or type(error).__name__


def _lock(handle):
    """Takes the writers' lock, giving up after LOCK_TIMEOUT so a stuck writer
    can't hang the app's ssh command. Closing the file releases it, even if
    this process dies."""
    deadline = time.time() + LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.time() >= deadline:
                return False
            time.sleep(0.05)


def _replace(path, text):
    """Write a whole new file, so mm-worker reads either the old rules or the new."""
    folder = os.path.dirname(path)
    temp = os.path.join(folder, ".%s.%d.tmp" % (os.path.basename(path), os.getpid()))
    handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(handle, 0o600)  # whatever the umask
        os.write(handle, text.encode("utf-8"))
        os.fsync(handle)
        os.close(handle)
        handle = None
        os.replace(temp, path)
    except BaseException:
        if handle is not None:
            os.close(handle)
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    try:
        handle = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(handle)  # so the rename itself survives a power cut
        finally:
            os.close(handle)
    except OSError:
        pass


def write_rules(rules, expected):
    """Write rules whose revision is expected + 1, if the file is still at expected."""
    try:
        os.makedirs(RULES_DIR, mode=0o700, exist_ok=True)
        lock = os.open(RULES_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        return {"ok": False, "error": "could not open the rules folder: " + _why(error)}
    try:
        if not _lock(lock):
            return {"ok": False, "error": "another change to the rules is still being written"}
        # An invalid file reads as the defaults, revision 0, as in snapshots.
        current, valid = load_rules()
        current = current["revision"] if valid else 0
        if current != expected:
            return {"ok": False, "error": "conflict", "revision": current}
        _replace(RULES_PATH, json.dumps(rules, indent=2) + "\n")
    except OSError as error:
        return {"ok": False, "error": "could not write the rules: " + _why(error)}
    finally:
        os.close(lock)
    return {"ok": True, "revision": rules["revision"]}


def rules_set(encoded, expected):
    if not REVISION.match(expected):
        return {"ok": False, "error": "the expected revision must be a whole number"}
    try:
        if len(encoded) > RULES_LIMIT * 2:
            raise ValueError
        rules = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8"))
    except (ValueError, RecursionError):  # bad base64 is a ValueError too
        return {"ok": False, "error": "the rules are not base64-encoded JSON"}
    if not isinstance(rules, dict):
        return {"ok": False, "error": "the rules must be a JSON object"}
    # The revision is ours to set, so whatever the app sent is replaced.
    rules["revision"] = int(expected) + 1
    error = rules_error(rules)
    if error:
        return {"ok": False, "error": error}
    if uses_full_names(rules) and dispatcher_version()[1] < FULL_NAMES_DISPATCHER:
        return {"ok": False, "error": "this machine's mm-worker is too old for these models: update it first"}
    if "cli" in rules and dispatcher_version()[1] < CLI_DISPATCHER:
        return {"ok": False, "error": "this machine's mm-worker is too old to choose a CLI: update it first"}
    if rules.get("cli") == "opencode" and not opencode_installed():
        return {"ok": False, "error": "opencode isn't installed on this machine"}
    return write_rules(rules, int(expected))


def uses_full_names(rules):
    return any(model not in MODELS[name] for choice in rules["levels"].values()
               for name, model in choice.items())


def rules_reset(expected):
    if not REVISION.match(expected):
        return {"ok": False, "error": "the expected revision must be a whole number"}
    return write_rules(default_rules(int(expected) + 1), int(expected))


def rules_command(argv):
    """One answer for every input, never a traceback: the app reads one line."""
    try:
        if argv[0] == "rules-set" and len(argv) == 3:
            return rules_set(argv[1], argv[2])
        if argv[0] == "rules-reset" and len(argv) == 2:
            return rules_reset(argv[1])
        return {"ok": False, "error": RULES_USAGE}
    except Exception as error:  # a bug here must still answer
        return {"ok": False, "error": "unexpected " + type(error).__name__}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A key goes to the host in the provider's env file, and nowhere else."""

    def redirect_request(self, *args, **kwargs):
        return None


def _model_page(url, key):
    request = urllib.request.Request(url, headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    opener = urllib.request.build_opener(_NoRedirect)
    with opener.open(request, timeout=MODEL_LIST_TIMEOUT) as response:
        return json.loads(response.read(1 << 20).decode("utf-8"))


def list_models(provider):
    """{"models": [...]} as the provider's API lists them, or {"error": "..."}.
    An error never repeats what the server said, so nothing odd reaches the app."""
    variable, path = MODEL_LISTS[provider]
    host = _env_value(provider, variable).rstrip("/")
    if not host.startswith("https://"):
        return {"error": "no https host configured"}
    key, models, after = _key_value(provider), [], None
    try:
        for _ in range(MODEL_LIST_PAGES):
            query = {"limit": 1000}
            if after:
                query["after_id"] = after
            page = _model_page(host + path + "?" + urllib.parse.urlencode(query), key)
            for entry in page["data"]:
                name = entry.get("id") if isinstance(entry, dict) else None
                if is_full_model_name(provider, name) and name not in models:
                    models.append(name)
            after = page.get("last_id") or page.get("lastId")
            if not (page.get("has_more") or page.get("hasMore")) or not after:
                break
    except urllib.error.HTTPError as error:
        return {"error": "HTTP %d" % error.code}
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        return {"error": "no usable answer"}
    return {"models": models}


def models_command():
    """One answer for every input, never a traceback: the app reads one line."""
    try:
        return {"ok": True, "providers": {name: list_models(name) for name in MODEL_LISTS if _key_value(name)}}
    except Exception as error:  # a bug here must still answer
        return {"ok": False, "error": "unexpected " + type(error).__name__}


def linux_cpu_times():
    """(busy, total) ticks since boot, from the first line of /proc/stat."""
    text = _read_text(PROC_STAT)
    if not text or not text.startswith("cpu "):
        return None
    try:
        # user nice system idle iowait irq softirq steal
        ticks = [int(field) for field in text.splitlines()[0].split()[1:9]]
    except ValueError:
        return None
    if len(ticks) < 5:
        return None
    total = sum(ticks)
    return total - ticks[3] - ticks[4], total


class _DarwinCPU:
    """(busy, total) ticks from Mach's host_statistics, a public interface."""

    HOST_CPU_LOAD_INFO = 3

    def __init__(self):
        self.libc = ctypes.CDLL(None)
        self.libc.mach_host_self.restype = ctypes.c_uint
        self.host = self.libc.mach_host_self()

    def __call__(self):
        info = (ctypes.c_uint * 4)()  # user, system, idle, nice
        count = ctypes.c_uint(4)
        status = self.libc.host_statistics(
            ctypes.c_uint(self.host), self.HOST_CPU_LOAD_INFO, info, ctypes.byref(count))
        if status != 0:
            return None
        total = sum(info)
        return total - info[2], total


class CPUSampler:
    """CPU use as a percentage of all cores, over the time between two reads."""

    def __init__(self, read=None):
        self.read = read if read is not None else self._reader()
        self.previous = self._read()

    @staticmethod
    def _reader():
        if sys.platform.startswith("linux"):
            return linux_cpu_times
        if sys.platform == "darwin":
            try:
                return _DarwinCPU()
            except (OSError, AttributeError):
                pass
        return lambda: None

    def _read(self):
        try:
            return self.read()
        except (OSError, ValueError, AttributeError):
            return None

    def percent(self):
        current = self._read()
        previous, self.previous = self.previous, current
        if not current or not previous:
            return None
        total = current[1] - previous[1]
        busy = current[0] - previous[0]
        if total <= 0 or busy < 0:
            return None  # no time passed, or a counter wrapped
        return round(min(100.0, 100.0 * busy / total), 1)


def _sensor_value(path):
    """A sysfs temperature in degrees, or None. They are stored in millidegrees."""
    text = _read_text(path, 32)
    try:
        value = int((text or "").strip()) / 1000.0
    except ValueError:
        return None
    return value if 0 < value < 130 else None


def linux_temperature():
    """CPU temperature in degrees: a CPU sensor driver, then a CPU thermal zone."""
    found = {}
    for folder in glob.glob(os.path.join(SYS_CLASS, "hwmon", "hwmon*")):
        name = (_read_text(os.path.join(folder, "name"), 64) or "").strip()
        if name not in LINUX_CPU_SENSORS:
            continue
        labelled, others = None, []
        for path in glob.glob(os.path.join(folder, "temp*_input")):
            value = _sensor_value(path)
            if value is None:
                continue
            label = (_read_text(path.replace("_input", "_label"), 64) or "").strip().lower()
            if label in LINUX_CPU_LABELS and labelled is None:
                labelled = value
            others.append(value)
        if labelled is not None or others:
            found[name] = labelled if labelled is not None else max(others)
    for name in LINUX_CPU_SENSORS:
        if name in found:
            return round(found[name], 1)
    zones = {}
    for folder in glob.glob(os.path.join(SYS_CLASS, "thermal", "thermal_zone*")):
        kind = (_read_text(os.path.join(folder, "type"), 64) or "").strip()
        value = _sensor_value(os.path.join(folder, "temp"))
        if value is not None:
            zones.setdefault(kind, value)
    for kind in LINUX_CPU_ZONES:
        if kind in zones:
            return round(zones[kind], 1)
    return None


def _darwin_sensors():
    """Sensor name to degrees, through IOKit's private HID event interface.

    Apple offers no public way to read this without root.
    """
    void = ctypes.c_void_p
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
    utf8 = 0x08000100
    temperature_event = 15

    cf.CFStringCreateWithCString.restype = void
    cf.CFStringCreateWithCString.argtypes = [void, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFNumberCreate.restype = void
    cf.CFNumberCreate.argtypes = [void, ctypes.c_long, void]
    cf.CFDictionaryCreate.restype = void
    cf.CFDictionaryCreate.argtypes = [void, void, void, ctypes.c_long, void, void]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetCount.argtypes = [void]
    cf.CFArrayGetValueAtIndex.restype = void
    cf.CFArrayGetValueAtIndex.argtypes = [void, ctypes.c_long]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [void, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    io.IOHIDEventSystemClientCreate.restype = void
    io.IOHIDEventSystemClientCreate.argtypes = [void]
    io.IOHIDEventSystemClientSetMatching.argtypes = [void, void]
    io.IOHIDEventSystemClientCopyServices.restype = void
    io.IOHIDEventSystemClientCopyServices.argtypes = [void]
    io.IOHIDServiceClientCopyProperty.restype = void
    io.IOHIDServiceClientCopyProperty.argtypes = [void, void]
    io.IOHIDServiceClientCopyEvent.restype = void
    io.IOHIDServiceClientCopyEvent.argtypes = [void, ctypes.c_int64, ctypes.c_int32, ctypes.c_int64]
    io.IOHIDEventGetFloatValue.restype = ctypes.c_double
    io.IOHIDEventGetFloatValue.argtypes = [void, ctypes.c_int32]

    def cfstr(text):
        return cf.CFStringCreateWithCString(None, text, utf8)

    def cfint(value):
        number = ctypes.c_int32(value)
        return cf.CFNumberCreate(None, 3, ctypes.byref(number))  # kCFNumberSInt32Type

    # Apple's vendor usage page for temperature sensors.
    keys = (void * 2)(cfstr(b"PrimaryUsagePage"), cfstr(b"PrimaryUsage"))
    values = (void * 2)(cfint(0xff00), cfint(5))
    matching = cf.CFDictionaryCreate(None, keys, values, 2, None, None)
    client = io.IOHIDEventSystemClientCreate(None)
    if not client:
        return {}
    io.IOHIDEventSystemClientSetMatching(client, matching)
    services = io.IOHIDEventSystemClientCopyServices(client)
    if not services:
        return {}
    product = cfstr(b"Product")
    sensors = {}
    for index in range(cf.CFArrayGetCount(services)):
        service = cf.CFArrayGetValueAtIndex(services, index)
        name_ref = io.IOHIDServiceClientCopyProperty(service, product)
        event = io.IOHIDServiceClientCopyEvent(service, temperature_event, 0, 0)
        if not name_ref or not event:
            continue
        buffer = ctypes.create_string_buffer(128)
        if cf.CFStringGetCString(name_ref, buffer, 128, utf8):
            sensors[buffer.value.decode("utf-8", "replace")] = io.IOHIDEventGetFloatValue(
                event, temperature_event << 16)
    return sensors


def pick_darwin_temperature(sensors):
    """The hottest CPU die reading. Calibration entries are constants, so skip them."""
    usable = {name: value for name, value in sensors.items()
              if 0 < value < 130 and "tcal" not in name.lower()}
    cpu = [value for name, value in usable.items()
           if "tdie" in name.lower() or "ACC MTR" in name or "CPU" in name]
    if cpu:
        return round(max(cpu), 1)
    other = [value for name, value in usable.items()
             if not re.search(r"battery|gas gauge|nand", name, re.IGNORECASE)]
    return round(max(other), 1) if other else None


def darwin_temperature(timeout=3.0):
    """Read Mac sensors in a forked child, so a change in Apple's private
    interface can cost a temperature reading but never the agent."""
    try:
        read_end, write_end = os.pipe()
        pid = os.fork()
    except OSError:
        return None
    if pid == 0:
        status = 1
        try:
            os.close(read_end)
            value = pick_darwin_temperature(_darwin_sensors())
            os.write(write_end, str(value).encode())
            status = 0
        finally:
            os._exit(status)
    os.close(write_end)
    try:
        ready, _, _ = select.select([read_end], [], [], timeout)
        text = os.read(read_end, 64).decode() if ready else ""
    except OSError:
        text = ""
    finally:
        os.close(read_end)
    try:
        if not text:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def read_temperature():
    if sys.platform.startswith("linux"):
        return linux_temperature()
    if sys.platform == "darwin":
        return darwin_temperature()
    return None


class Vitals:
    """CPU use and temperature, with temperature read less often."""

    def __init__(self, read_temperature=read_temperature):
        self.cpu = CPUSampler()
        self.read_temperature = read_temperature
        self.temperature = None
        self.temperature_at = None

    def read(self, now):
        if self.temperature_at is None or now - self.temperature_at >= TEMPERATURE_INTERVAL:
            self.temperature_at = now
            try:
                self.temperature = self.read_temperature()
            except (OSError, ValueError, AttributeError):
                self.temperature = None
        return {"cpuPercent": self.cpu.percent(), "temperature": self.temperature}


def _text_or_none(value):
    return value if isinstance(value, str) and value else None


def read_claude_account():
    """Email, plan and organization from oauthAccount in ~/.claude.json, and
    nothing else from it: the file can hold keys and tokens. None means not
    known, as a machine can also log in with a token."""
    try:
        if os.path.getsize(CLAUDE_CONFIG) > ACCOUNT_LIMIT:
            return None
        with open(CLAUDE_CONFIG, "rb") as handle:
            data = handle.read(ACCOUNT_LIMIT + 1)
        if len(data) > ACCOUNT_LIMIT:
            return None
        account = json.loads(data.decode("utf-8")).get("oauthAccount")
    except (OSError, ValueError, RecursionError, AttributeError):
        return None
    if not isinstance(account, dict) or not _text_or_none(account.get("emailAddress")):
        return None
    return {
        "email": account["emailAddress"],
        "plan": _text_or_none(account.get("planDisplayName")),
        "organization": _text_or_none(account.get("organizationName")),
    }


class ClaudeAccount:
    """read_claude_account at most every ACCOUNT_INTERVAL, and only parsed
    again when the file changed, as it can be large."""

    def __init__(self):
        self.checked_at = None
        self.stamp = None
        self.account = None

    def read(self, now):
        if self.checked_at is not None and now - self.checked_at < ACCOUNT_INTERVAL:
            return self.account
        self.checked_at = now
        try:
            stat = os.stat(CLAUDE_CONFIG)
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        except OSError:
            stamp = None
        if stamp is None or stamp != self.stamp:
            self.stamp = stamp
            self.account = read_claude_account() if stamp else None
        return self.account


def os_user():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return os.environ.get("USER") or os.environ.get("LOGNAME") or os.path.basename(HOME)


def host_info():
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    return {
        "hostname": socket.gethostname().split(".")[0],
        "user": os_user(),
        "system": platform.system(),
        "machine": platform.machine(),
        "cpus": os.cpu_count() or 0,
        "load": round(load, 2) if load is not None else None,
    }


def snapshot(log, now, vitals=None, dispatcher=None, history=None, account=None):
    """One snapshot. usageHistory is in it only when it is news (see UsageHistory.news)."""
    sessions, models = log.report(now, history)
    workers = (local_workers(now, log.streams, log.usage_by_session)
               + remote_jobs(now, log.streams, log.usage_by_session))
    live = {worker.pop("stream") for worker in workers if "stream" in worker}
    log.streams.forget_except(live)
    host = host_info()
    if vitals is not None:
        host.update(vitals.read(now))
    report = {
        "agentVersion": AGENT_VERSION,
        "host": host,
        "claudeAccount": (account if account is not None else ClaudeAccount()).read(now),
        "sessions": sessions,
        "todayByModel": models,
        "workers": workers,
        "dispatcher": (dispatcher if dispatcher is not None else Dispatcher()).read(now),
    }
    news = history.news(now) if history is not None else None
    if news is not None:
        report["usageHistory"] = news
    return report


def main(argv):
    if argv[:1] == ["cancel"]:
        print(json.dumps(cancel(argv[1] if len(argv) > 1 else "")))
        return 0
    if argv[:1] in (["rules-set"], ["rules-reset"]):
        print(json.dumps(rules_command(argv)))
        return 0
    if argv[:1] == ["models"]:
        print(json.dumps(models_command()))
        return 0
    if hasattr(os, "nice"):
        try:
            os.nice(10)  # this should never be what a machine is busy doing
        except OSError:
            pass
    log = SessionLog()
    history = UsageHistory()
    vitals = Vitals()
    dispatcher = Dispatcher()
    account = ClaudeAccount()
    time.sleep(0.25)  # so the first CPU reading has an interval to measure
    once = "--once" in argv
    last_body = None
    last_sent = 0.0
    while True:
        now = time.time()
        log.scan(now)
        if once:
            history.finish(now)
        report = snapshot(log, now, vitals, dispatcher, history, account)
        # Ages and machine load change on every pass, so they don't count as news.
        body = json.dumps(_without_clock(report), sort_keys=True)
        if once or "usageHistory" in report or body != last_body or now - last_sent >= HEARTBEAT:
            try:
                sys.stdout.write(json.dumps(report, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                return 0
            last_body, last_sent = body, now
        if once:
            return 0
        # After the snapshot is out, so a long scan only ever delays the next one by a pass.
        history.step(time.time())
        time.sleep(SCAN_INTERVAL)


def _without_clock(report):
    stable = dict(report)
    # Sent only when it is news already, and left out of most snapshots.
    stable.pop("usageHistory", None)
    stable["host"] = {k: v for k, v in report["host"].items() if k not in ("load", "cpuPercent", "temperature")}
    stable["sessions"] = [{k: v for k, v in s.items() if k != "idleSeconds"} for s in report["sessions"]]
    stable["workers"] = [{k: v for k, v in w.items() if k not in ("elapsedSeconds", "idleSeconds")} for w in report["workers"]]
    # A slot comes and goes with its worker, which is news already. Leaving
    # `used` out means a slot still waiting for its pid waits for the heartbeat.
    dispatcher = dict(report["dispatcher"])
    dispatcher["providers"] = {name: {k: v for k, v in p.items() if k != "used"}
                               for name, p in report["dispatcher"]["providers"].items()}
    stable["dispatcher"] = dispatcher
    return stable


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
