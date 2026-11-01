#!/usr/bin/env python3
"""TokNotch Fleet agent: reports what agents are doing on this machine.

TokNotch pipes this file into `python3 -I -u - watch` over ssh, so nothing is
installed on the remote machine and the agent always matches the app. It ends
by itself once the ssh connection goes away, because its next write fails.

It is read-only. It reports token counts, model names, tool names and a short
title per session or worker. It never reports message bodies, tool inputs or
anything from credential files.

Standard library only, Python 3.9 or newer.

Output is one JSON object per line. `--once` prints a single snapshot.
"""

import json
import os
import platform
import re
import socket
import sys
import time

AGENT_VERSION = 1
SCAN_INTERVAL = 2.0  # how often known session files are checked for growth
WALK_INTERVAL = 15.0  # how often the tree is walked for new session files
HEARTBEAT = 10.0  # longest gap between snapshots, so silence means trouble
SESSION_WINDOW = 6 * 3600  # sessions quieter than this are left out of the list
MAX_SESSIONS = 60
FINISHED_JOB_WINDOW = 3600  # how long a finished worker stays listed
JOB_WINDOW = 7 * 86400  # older job folders are ignored without being read
TITLE_LENGTH = 60
READ_CHUNK = 1 << 20

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")
WORKER_STATE = os.path.join(HOME, ".cache", "mm-worker")


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
            "when": known["when"] if known and known["when"] else when,
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

    def report(self, now):
        midnight = local_midnight(now)
        by_session = {}
        by_model = {}
        for record in self.messages.values():
            bucket = by_session.setdefault(record["session"], _empty_usage())
            _add(bucket, record)
            if record["when"] is not None and record["when"] >= midnight:
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
        listed.sort(key=lambda item: item["idleSeconds"])
        return listed[:MAX_SESSIONS], by_model


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by somebody else
    return True


def _worker_result(path):
    """Tokens from a finished worker's final JSON line. Never its result text."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(max(0, size - (1 << 20)))
            lines = handle.read().splitlines()
        usage = json.loads(lines[-1]).get("usage") or {}
    except (OSError, ValueError, IndexError, AttributeError):
        return None
    return {
        "input": _count(usage, "input_tokens"),
        "output": _count(usage, "output_tokens"),
        "cacheRead": _count(usage, "cache_read_input_tokens"),
        "cacheWrite": _count(usage, "cache_creation_input_tokens"),
    }


def remote_jobs(now):
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
            "title": tidy_title(_read_text(os.path.join(folder, "prompt.md"))),
            "elapsedSeconds": int((finished or now) - started),
        }
        if finished is not None:
            usage = _worker_result(os.path.join(folder, "out.json"))
            if usage:
                job["usage"] = usage
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


def local_workers(now):
    """Workers mm-worker holds a slot for on this machine, and any it sent away."""
    root = os.path.join(WORKER_STATE, "slots")
    runs = os.path.join(WORKER_STATE, "runs")
    workers = []
    try:
        providers = os.listdir(root)
    except OSError:
        return workers
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
            run_id = _run_id_for(runs, pid)
            try:
                started = os.stat(pid_path).st_mtime
            except OSError:
                continue
            worker = {
                "id": run_id or pid,
                "provider": provider,
                "state": "running",
                "title": tidy_title(_read_text(os.path.join(runs, str(run_id) + ".prompt.md"))),
                "elapsedSeconds": int(now - started),
            }
            remote = _read_text(os.path.join(runs, str(run_id) + ".remote")) if run_id else None
            if remote and remote.split():
                worker["remoteHost"] = remote.split()[0]
            workers.append(worker)
    return workers


def _run_id_for(runs, pid):
    """A run id ends in the dispatcher's pid, and the newest match is the live one."""
    suffix = "-" + pid + ".prompt.md"
    try:
        matches = [name for name in os.listdir(runs) if name.endswith(suffix)]
    except OSError:
        return None
    return max(matches)[: -len(".prompt.md")] if matches else None


def host_info():
    try:
        load = os.getloadavg()[0]
    except (AttributeError, OSError):
        load = None
    return {
        "hostname": socket.gethostname().split(".")[0],
        "system": platform.system(),
        "machine": platform.machine(),
        "cpus": os.cpu_count() or 0,
        "load": round(load, 2) if load is not None else None,
    }


def snapshot(log, now):
    sessions, models = log.report(now)
    return {
        "agentVersion": AGENT_VERSION,
        "host": host_info(),
        "sessions": sessions,
        "todayByModel": models,
        "workers": local_workers(now) + remote_jobs(now),
    }


def main(argv):
    if hasattr(os, "nice"):
        try:
            os.nice(10)  # this should never be what a machine is busy doing
        except OSError:
            pass
    log = SessionLog()
    once = "--once" in argv
    last_body = None
    last_sent = 0.0
    while True:
        now = time.time()
        log.scan(now)
        report = snapshot(log, now)
        # Ages and load change on every pass, so they don't count as news.
        body = json.dumps(_without_clock(report), sort_keys=True)
        if once or body != last_body or now - last_sent >= HEARTBEAT:
            try:
                sys.stdout.write(json.dumps(report, separators=(",", ":")) + "\n")
                sys.stdout.flush()
            except BrokenPipeError:
                return 0
            last_body, last_sent = body, now
        if once:
            return 0
        time.sleep(SCAN_INTERVAL)


def _without_clock(report):
    stable = dict(report)
    stable["host"] = {k: v for k, v in report["host"].items() if k != "load"}
    stable["sessions"] = [{k: v for k, v in s.items() if k != "idleSeconds"} for s in report["sessions"]]
    stable["workers"] = [{k: v for k, v in w.items() if k != "elapsedSeconds"} for w in report["workers"]]
    return stable


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
