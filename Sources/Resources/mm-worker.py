#!/usr/bin/env python3
MM_WORKER_VERSION = 6

# mm-worker: runs one non-interactive Claude Code task on one of several
# providers, chosen by task level and load, and moves to the next provider when
# one runs out of quota or fails.
#
# The rules for this machine are in ~/.config/mm-worker/rules.json, described in
# docs/FLEET-RULES.md together with the state layout under ~/.cache/mm-worker.
# fleet-agent.py reads that layout, so keep it exact.
#
# A task sent to another machine obeys that machine's rules. This file is copied
# there and run with --remote, so both sides always agree on what the rules mean.
#
# Standard library only, Python 3.9 or newer, Linux and macOS.

import errno
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from fractions import Fraction

USAGE = """\
usage: mm-worker [options] "prompt"
       mm-worker [options] -f prompt.md
       echo "prompt" | mm-worker [options]
       any of these may end with -- <claude arguments...>

  -l, --level simple|normal|hard|extreme
                                   task level (default normal, or simple with -m fast)
  -b, -P, --backend auto|minimax|zhipu|claude|qwen|codex
                                   force a provider, even a disabled one (default auto)
  -m, --mm-model m3|m27|fast|NAME  MiniMax model
  -z, --zp-model flash|NAME        Zhipu model
  -q, --qw-model glm|max|flash|lite|ds
                                   Qwen endpoint model
  -c, --claude-model MODEL         Claude model (opus only for hard and extreme,
                                   fable only for extreme)
      --codex-model default|NAME   Codex model (default: codex chooses); codex runs on
                                   this machine only; after --, only --allowedTools
      --cli claude|opencode        Agent to run the worker with (or MM_WORKER_CLI, or the
                                   rule "cli"; default claude). opencode: minimax, zhipu,
                                   qwen on this machine; after --, only --allowedTools
  -t, --timeout SEC                timeout for one run (default from rules)
  -n, --name NAME                  task name shown in Fleet
  -H, --host HOST                  run the worker on another machine over ssh, under its rules
  -C, --rdir DIR                   working folder there (default: this one, relative to home)
  --json                           print the raw result event instead of its text
  --status                         show cooling, slots and recent runs
  --fetch RUN_ID                   fetch the result of a task sent to another machine
  --kill RUN_ID                    cancel a task sent to another machine

stdout carries only the result; logs go to stderr. Exit codes: 0 success,
124 timeout, 130 cancelled or interrupted, anything else failure.
"""

PROVIDERS = ("minimax", "zhipu", "claude", "qwen", "codex")
LEVELS = ("simple", "normal", "hard", "extreme")
# Rules written before extreme existed leave it out. Such a file stays valid,
# and the level takes its default.
OPTIONAL_LEVELS = ("extreme",)
MODELS = {
    "minimax": ("m3", "m27", "fast"),
    "zhipu": ("flash",),
    "claude": ("sonnet", "haiku", "claude-opus-5-5", "claude-fable-5-2"),
    "qwen": ("glm", "max", "flash", "lite", "ds"),
    "codex": ("default",),  # pass no -m: codex chooses
}

# The command-line agents that can run a worker.
CLIS = ("claude", "opencode")

# Besides the short names above, a rule may name a model as the provider's own
# API does. The prefix keeps one provider's model off another, and keeps a key
# pasted into a rules file from passing as a model.
MODEL_PREFIXES = {
    "minimax": ("minimax-",),
    "zhipu": ("glm-",),
    "claude": ("claude-",),
    "qwen": ("qwen", "glm-", "deepseek-"),
    "codex": ("gpt-", "codex-"),
}
FULL_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")


def is_full_model_name(provider, model):
    return (isinstance(model, str) and FULL_MODEL_NAME.fullmatch(model) is not None
            and model.lower().startswith(MODEL_PREFIXES[provider]))


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
        "codex": {"enabled": False, "slots": 4},
    },
    "levels": {
        "simple": {"minimax": "fast", "zhipu": "flash", "claude": "sonnet"},
        "normal": {"minimax": "m3", "zhipu": "flash", "claude": "sonnet"},
        "hard": {"claude": "claude-opus-5-5"},
        "extreme": {"claude": "claude-fable-5-2"},
    },
    "defaults": {"timeoutSeconds": 900, "cooldownSeconds": 1800},
}

# With -b, a provider the level's rules leave out still needs a model.
LEVEL_FALLBACK = {
    "extreme": {"minimax": "m3", "zhipu": "flash", "claude": "claude-fable-5-2", "qwen": "glm",
                "codex": "default"},
    "hard": {"minimax": "m3", "zhipu": "flash", "claude": "claude-opus-5-5", "qwen": "glm",
             "codex": "default"},
    "normal": {"minimax": "m3", "zhipu": "flash", "claude": "sonnet", "qwen": "flash",
               "codex": "default"},
    "simple": {"minimax": "fast", "zhipu": "flash", "claude": "sonnet", "qwen": "ds",
               "codex": "default"},
}

# Each provider maps Claude's model aliases to its own models (see provider_env),
# so a model is chosen by passing the alias. A full model name passes as it is.
ALIASES = {
    "minimax": {"m3": "opus[1m]", "m27": "sonnet", "fast": "haiku"},
    "zhipu": {"flash": "sonnet"},
    "qwen": {"glm": "glm-5.3[1m]", "max": "opus", "flash": "sonnet", "lite": "haiku",
             "ds": "deepseek-v4-pro-0813[1m]"},
    "claude": {},
    "codex": {},
}

KEY_VARS = {
    "minimax": ("MINIMAX_API_HOST", "MINIMAX_API_KEY"),
    "zhipu": ("ZHIPU_API_HOST", "ZHIPU_API_KEY"),
    "qwen": ("QWEN_API_HOST", "QWEN_API_KEY"),
}
# Path below the host, model and timeout of the one-token probe request.
PROBES = {
    "minimax": ("/anthropic/v1/messages", "MiniMax-M2.7-highspeed", 20),
    "zhipu": ("/v1/messages", "glm-5.3-flash", 30),
    "qwen": ("/v1/messages", "qwen3.6-flash", 20),
}

# A parent session pointed at another provider would otherwise leak into workers.
SCRUBBED = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
            "API_TIMEOUT_MS", "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_QWEN_SESSION")
CLAUDE_FALLBACKS = ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")
OPENCODE_FALLBACKS = ("~/.local/bin/opencode", "~/.opencode/bin/opencode", "/opt/homebrew/bin/opencode",
                      "/usr/local/bin/opencode")
CODEX_FALLBACKS = ("~/.npm-global/bin/codex", "~/.local/bin/codex", "/opt/homebrew/bin/codex",
                   "/usr/local/bin/codex")

# Quota errors as the providers word them, including their Chinese texts for
# balance, quota, limit and expiry, and their numeric quota codes.
QUOTA_RE = re.compile(
    r'usage limit|limit reached|quota|insufficient|arrearage|exceed|余额|额度|上限'
    r'|"status_code":(1008|2056)|到期|"code":"?(1113|1308|1309|1310)', re.IGNORECASE)
PROBE_ERROR_RE = re.compile(r'"status_code":[1-9]', re.IGNORECASE)

FAILOVER_NOTE = ("Note: another worker started this task and was interrupted partway. The working "
                 "tree may already hold some of its changes. Check the current state first, build "
                 "on it, and don't redo what is already done.\n\n")

RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]+$")
FAILOVER = 75
CANCELLED = "cancelled"

# Seconds. Module level so the tests can shorten them.
KILL_GRACE = 15  # from SIGTERM to SIGKILL for a worker that won't stop
STRAGGLER_GRACE = 2  # for processes a finished worker left behind
QUEUE_SLEEP = 5
LOCK_STALE = 30
SLOT_GRACE = 5  # a slot folder with no pid yet is being taken, not dead
RETRY_SLEEP = 30  # between ssh attempts while the other machine is unreachable
SUBMIT_GIVE_UP = 600
UNFINISHED_START = 60  # a job folder without a claim: its starter may still be working
QUEUED_KEEP = 7 * 86400  # what a dispatcher that vanished left in queued/
POLL_DEFAULT = 20
OFFLINE_WAIT_DEFAULT = 21600


def log(text):
    try:
        sys.stderr.write("[mm-worker] %s\n" % text)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


class Exit(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class UsageError(Exception):
    pass


class Interrupted(BaseException):
    """Raised by the signal handler, so ordinary error handling can't swallow it."""


# ---------- rules ----------

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _exact_keys(obj, keys, where):
    if not isinstance(obj, dict):
        return "%s must be an object" % where
    unknown = sorted(set(obj) - set(keys))
    if unknown:
        return "unknown key %s in %s" % (unknown[0], where)
    missing = [key for key in keys if key not in obj]
    if missing:
        return "missing %s in %s" % (missing[0], where)
    return None


def validate_rules(rules):
    """None if the rules are valid, otherwise what is wrong with them."""
    # cli is optional, and absent means claude, so the defaults stay as they were.
    required = {key: value for key, value in rules.items() if key != "cli"} if isinstance(rules, dict) else rules
    error = _exact_keys(required, ("version", "revision", "balance", "order", "providers", "levels",
                                   "defaults"), "rules")
    if error:
        return error
    if rules.get("cli", "claude") not in CLIS:
        return "cli must be one of %s" % ", ".join(CLIS)
    if not _is_int(rules["version"]) or rules["version"] != 1:
        return "version must be 1"
    if not _is_int(rules["revision"]) or rules["revision"] < 0:
        return "revision must be a whole number"
    if rules["balance"] not in ("proportional", "priority"):
        return "balance must be proportional or priority"
    providers = rules["providers"]
    if not isinstance(providers, dict) or not providers:
        return "providers must be a non-empty object"
    for name, entry in providers.items():
        if name not in PROVIDERS:
            return "unknown provider %s" % name
        error = _exact_keys(entry, ("enabled", "slots"), "providers.%s" % name)
        if error:
            return error
        if not isinstance(entry["enabled"], bool):
            return "providers.%s.enabled must be true or false" % name
        if not _is_int(entry["slots"]) or not 1 <= entry["slots"] <= 32:
            return "providers.%s.slots must be 1 to 32" % name
    # The contract's own defaults leave the disabled qwen out of order, so a
    # disabled provider may be missing. An enabled one may not: it would never
    # get automatic work, and nothing would say why.
    order = rules["order"]
    if (not isinstance(order, list) or not all(isinstance(name, str) for name in order)
            or len(order) != len(set(order))):
        return "order must list providers, each once"
    for name in order:
        if name not in providers:
            return "order names unknown provider %s" % name
    for name, entry in providers.items():
        if entry["enabled"] and name not in order:
            return "order must include the enabled provider %s" % name
    levels = rules["levels"]
    required_levels = tuple(level for level in LEVELS
                            if level not in OPTIONAL_LEVELS or (isinstance(levels, dict) and level in levels))
    error = _exact_keys(levels, required_levels, "levels")
    if error:
        return error
    for level in required_levels:
        models = levels[level]
        if not isinstance(models, dict) or not models:
            return "levels.%s must name at least one provider" % level
        for name, model in models.items():
            if name not in providers:
                return "levels.%s names unknown provider %s" % (level, name)
            if model not in MODELS[name] and not is_full_model_name(name, model):
                return "levels.%s.%s: %s is not a %s model" % (level, name, model, name)
    error = _exact_keys(rules["defaults"], ("timeoutSeconds", "cooldownSeconds"), "defaults")
    if error:
        return error
    timeout = rules["defaults"]["timeoutSeconds"]
    if not _is_int(timeout) or not 30 <= timeout <= 86400:
        return "defaults.timeoutSeconds must be 30 to 86400"
    cooldown = rules["defaults"]["cooldownSeconds"]
    if not _is_int(cooldown) or not 0 <= cooldown <= 86400:
        return "defaults.cooldownSeconds must be 0 to 86400"
    return None


def load_rules(path):
    """(rules, error). A missing file is the defaults; an invalid one is too, with the error."""
    try:
        with open(path, "rb") as handle:
            text = handle.read()
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_RULES)), None
    except OSError as error:
        return json.loads(json.dumps(DEFAULT_RULES)), "cannot read it: %s" % error.strerror
    try:
        rules = json.loads(text.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        return json.loads(json.dumps(DEFAULT_RULES)), "not valid JSON: %s" % error
    problem = validate_rules(rules)
    if problem:
        return json.loads(json.dumps(DEFAULT_RULES)), problem
    for level in OPTIONAL_LEVELS:
        rules["levels"].setdefault(level, dict(DEFAULT_RULES["levels"][level]))
    return rules, None


# ---------- provider environment ----------

ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _shell_value(raw):
    """The value of a shell assignment, quotes removed. Nothing is expanded or run."""
    out, i = [], 0
    while i < len(raw):
        char = raw[i]
        if char == "'":
            end = raw.find("'", i + 1)
            end = len(raw) if end < 0 else end
            out.append(raw[i + 1:end])
            i = end + 1
        elif char == '"':
            i += 1
            while i < len(raw) and raw[i] != '"':
                if raw[i] == "\\" and i + 1 < len(raw) and raw[i + 1] in '"\\$`':
                    out.append(raw[i + 1])
                    i += 2
                else:
                    out.append(raw[i])
                    i += 1
            i += 1
        elif char.isspace():
            break  # the rest is a comment or another command
        elif char == "\\" and i + 1 < len(raw):
            out.append(raw[i + 1])
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def read_env_file(path):
    values = {}
    try:
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return values
    for line in text.splitlines():
        match = ENV_LINE.match(line)
        if match:
            values[match.group(1)] = _shell_value(match.group(2))
    return values


def env_file(provider):
    return os.path.join(os.path.expanduser("~"), ".config", provider, "env")


def provider_env(provider, base):
    """The environment a local worker runs with, as mm-remote-run sets it up."""
    env = dict(base)
    if provider == "claude":
        return env
    env.update(read_env_file(env_file(provider)))

    def pick(name, default):
        return env.get(name) or default

    if provider == "minimax":
        env["ANTHROPIC_BASE_URL"] = env.get("MINIMAX_API_HOST", "") + "/anthropic"
        env["ANTHROPIC_AUTH_TOKEN"] = env.get("MINIMAX_API_KEY", "")
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = pick("MM_OPUS", "MiniMax-M3")
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = pick("MM_SONNET", "MiniMax-M2.7")
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = pick("MM_HAIKU", "MiniMax-M2.7-highspeed")
        env["API_TIMEOUT_MS"] = "3000000"
        env["CLAUDE_CODE_MODEL_CAPABILITIES"] = "minimax*=-mid_conv_tool_change"
    elif provider == "qwen":
        env["ANTHROPIC_BASE_URL"] = env.get("QWEN_API_HOST", "")
        env["ANTHROPIC_AUTH_TOKEN"] = env.get("QWEN_API_KEY", "")
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = pick("QW_OPUS", "qwen3.8-max[1m]")
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = pick("QW_SONNET", "qwen3.8-flash[1m]")
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = pick("QW_HAIKU", "qwen3.6-flash[1m]")
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = pick("QW_MAX_OUTPUT", "65536")
        env["API_TIMEOUT_MS"] = "3000000"
        env["CLAUDE_CODE_MODEL_CAPABILITIES"] = (
            "qwen*=-mid_conv_tool_change;glm*=-mid_conv_tool_change;"
            "deepseek*=-mid_conv_tool_change;auto=-mid_conv_tool_change")
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = pick("QW_SUBAGENT", "sonnet")
    elif provider == "zhipu":
        env["ANTHROPIC_BASE_URL"] = env.get("ZHIPU_API_HOST", "")
        env["ANTHROPIC_AUTH_TOKEN"] = env.pop("ZHIPU_API_KEY", "")
        # Only GLM-5.3-Flash is used, so every alias points at it.
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = pick("ZP_OPUS", "glm-5.3-flash")
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = pick("ZP_SONNET", "glm-5.3-flash")
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = pick("ZP_HAIKU", "glm-5.3-flash")
        env["API_TIMEOUT_MS"] = "3000000"
        env["CLAUDE_CODE_MODEL_CAPABILITIES"] = "glm*=-mid_conv_tool_change"
        env["CLAUDE_CODE_SUBAGENT_MODEL"] = pick("ZP_SUBAGENT", "sonnet")
    return env


def has_key(provider):
    if provider == "claude":
        return True
    if provider == "codex":
        return find_codex() is not None and codex_signed_in()
    values = dict(os.environ)
    values.update(read_env_file(env_file(provider)))
    return bool(values.get(KEY_VARS[provider][1], "").strip())


def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    for path in CLAUDE_FALLBACKS:
        path = os.path.expanduser(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def find_opencode():
    found = shutil.which("opencode")
    if found:
        return found
    for path in OPENCODE_FALLBACKS:
        path = os.path.expanduser(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def find_codex():
    found = shutil.which("codex")
    if found:
        return found
    for path in CODEX_FALLBACKS:
        path = os.path.expanduser(path)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def codex_signed_in():
    """Whether codex has something to sign in with. Only whether the file exists:
    it holds a credential, so it is never opened."""
    homes = [os.environ.get("CODEX_HOME"), os.path.join(os.path.expanduser("~"), ".codex")]
    if any(home and os.path.isfile(os.path.join(home, "auth.json")) for home in homes):
        return True
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


# ---------- opencode ----------

# opencode's own id for a provider must not be "minimax": its built-in entry of
# that name hides or breaks models. Host variable, path below it, key variable.
OPENCODE_BACKENDS = {
    "minimax": ("mm", "MINIMAX_API_HOST", "/anthropic/v1", "MINIMAX_API_KEY"),
    "zhipu": ("zp", "ZHIPU_API_HOST", "/v1", "ZHIPU_API_KEY"),
    "qwen": ("qw", "QWEN_API_HOST", "/v1", "QWEN_API_KEY"),
}
# What the short names mean, as ALIASES does for claude through provider_env.
MODEL_NAMES = {
    "minimax": {"m3": "MiniMax-M3", "m27": "MiniMax-M2.7", "fast": "MiniMax-M2.7-highspeed"},
    "zhipu": {"flash": "glm-5.3-flash"},
    "qwen": {"glm": "glm-5.3", "max": "qwen3.8-max", "flash": "qwen3.8-flash",
             "lite": "qwen3.6-flash", "ds": "deepseek-v4-pro-0813"},
}
# claude's tool names, and the opencode permission each one opens.
OPENCODE_TOOLS = {
    "Read": "read", "Glob": "glob", "Grep": "grep", "LS": "list", "Edit": "edit", "Write": "edit",
    "MultiEdit": "edit", "NotebookEdit": "edit", "WebFetch": "webfetch", "WebSearch": "websearch",
    "Task": "task", "TodoWrite": "todowrite",
}
# opencode matches a command's text by prefix, so `echo *` also lets through
# `echo x > file` and `echo $(touch file)`, and a worker told no will try exactly
# that. The last matching rule wins, so these go after the allowed commands.
BASH_ESCAPES = ("*>*", "*<*", "*$(*", "*`*", "*;*", "*&*", "*|*", "*\n*")
TOOL_SPEC = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\([^)]*\))?")


def allowed_tools(extra, agent):
    """The tool names --allowedTools gives in claude-style arguments after `--`. An
    argument with no equivalent for the agent is an error, as dropping it could widen
    what the worker may do."""
    specs, i = [], 0
    while i < len(extra):
        arg = extra[i]
        i += 1
        if arg.startswith(("--allowedTools=", "--allowed-tools=")):
            specs.append(arg.split("=", 1)[1])
        elif arg in ("--allowedTools", "--allowed-tools"):
            while i < len(extra) and not extra[i].startswith("-"):
                specs.append(extra[i])
                i += 1
        else:
            raise UsageError("%s has no equivalent of %s" % (agent, arg))
    return [tool for spec in specs for tool in TOOL_SPEC.findall(spec)]


def opencode_permission(extra):
    """The permissions for claude-style arguments after `--`, as tight as claude -p:
    reading is open, and everything else is denied unless --allowedTools names it.
    With no permissions at all, opencode would let a worker write anything, so every
    run sets them, each by name so that no opencode.json can open one up."""
    tools = allowed_tools(extra, "opencode")
    permission = {"*": "deny", "edit": "deny", "bash": "deny", "task": "deny", "webfetch": "deny",
                  "websearch": "deny", "todowrite": "deny", "skill": "deny", "lsp": "deny",
                  "question": "deny", "doom_loop": "deny",
                  "read": {"*": "allow", "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow"},
                  "glob": "allow", "grep": "allow", "list": "allow",
                  # Each tool's own permission still decides; prompts use absolute paths.
                  "external_directory": "allow"}
    commands = None
    for tool in tools:
        if tool == "Bash":
            commands = "all"
        elif tool.startswith("Bash("):
            if commands != "all":
                commands = commands or {"*": "deny"}
                inner = tool[5:-1].strip()
                if inner.endswith(":*"):
                    commands[inner[:-2]] = "allow"
                    commands[inner[:-2] + " *"] = "allow"
                else:
                    commands[inner] = "allow"
        elif tool in OPENCODE_TOOLS:
            if OPENCODE_TOOLS[tool] != "read":
                permission[OPENCODE_TOOLS[tool]] = "allow"
        else:
            log("opencode worker: %s has no equivalent and stays denied" % tool)
    if commands == "all":
        permission["bash"] = "allow"
    elif commands:
        permission["bash"] = dict(commands, **{escape: "deny" for escape in BASH_ESCAPES})
    return permission


def opencode_config(provider, model, permission, values):
    """The whole configuration of one run, so a machine needs no opencode.json."""
    pid, host_var, path, key_var = OPENCODE_BACKENDS[provider]
    name = MODEL_NAMES[provider].get(model, model)
    context = 1000000 if name.startswith("MiniMax-M3") else 200000
    return {
        "enabled_providers": [pid],
        "model": "%s/%s" % (pid, name),
        "small_model": "%s/%s" % (pid, name),
        "autoupdate": False,
        "share": "disabled",
        "snapshot": False,
        "permission": permission,
        "provider": {pid: {
            "name": provider,
            "npm": "@ai-sdk/anthropic",
            "options": {"baseURL": values.get(host_var, "") + path, "apiKey": "{env:%s}" % key_var},
            "models": {name: {"name": name, "limit": {"context": context, "output": 32000}}},
        }},
    }


def opencode_args(executable, provider, model):
    name = MODEL_NAMES[provider].get(model, model)
    return [executable, "run", "--format", "json", "-m", "%s/%s" % (OPENCODE_BACKENDS[provider][0], name)]


def opencode_env(provider, model, permission, base):
    env = dict(base)
    env.update(read_env_file(env_file(provider)))
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(opencode_config(provider, model, permission, env))
    return env


def finish_opencode_stream(path):
    """Add the result line claude ends its stream with, so everything after this
    treats both alike. A stream that just stops gets none, which reads as a failure."""
    text, tokens_in, tokens_out, reason, error = [], 0, 0, None, None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                part = event.get("part") if isinstance(event.get("part"), dict) else {}
                kind = event.get("type")
                if kind == "step_start":
                    text = []  # the answer is what the last step said
                elif kind == "text" and isinstance(part.get("text"), str):
                    text.append(part["text"])
                elif kind == "step_finish":
                    reason = part.get("reason")
                    tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
                    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                    for field, source in (("input", tokens), ("read", cache), ("write", cache)):
                        if isinstance(source.get(field), int):
                            tokens_in += source[field]
                    if isinstance(tokens.get("output"), int):
                        tokens_out += tokens["output"]
                elif kind == "error":
                    error = event.get("error") if isinstance(event.get("error"), dict) else {}
    except OSError:
        return
    usage = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    if error is not None:
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        status = data.get("statusCode")
        message = str(data.get("message") or error.get("name") or "unknown error")[:500]
        result = {"type": "result", "is_error": True, "result": message, "usage": usage,
                  # Any status: an opencode error means the task didn't run, so another provider may.
                  "api_error_status": status if isinstance(status, int) else "opencode"}
    elif reason in ("stop", "end_turn", "length") and text:
        result = {"type": "result", "is_error": False, "result": "".join(text), "usage": usage}
    else:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")


# ---------- codex ----------

CODEX_READ_TOOLS = ("Read", "Glob", "Grep", "LS")
CODEX_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")


def codex_sandbox(extra):
    """(sandbox, notes to log) for claude-style arguments after `--`: the tightest
    sandbox that fits what --allowedTools names. Codex can't allow single commands,
    so Bash(cmd:*) runs anything the sandbox lets through, which read-only still holds."""
    sandbox, notes, commands = "read-only", [], False
    for tool in allowed_tools(extra, "codex"):
        if tool in CODEX_WRITE_TOOLS:
            sandbox = "workspace-write"
        elif tool.startswith("Bash("):
            commands = True
        elif tool not in CODEX_READ_TOOLS:
            notes.append("codex worker: %s has no equivalent and stays denied" % tool)
    if commands:
        notes.append("codex worker: codex can't allow single commands, so Bash(...) lets it run "
                     "any command inside the %s sandbox" % sandbox)
    return sandbox, notes


def codex_args(executable, sandbox, model):
    # --ignore-user-config: the user's config.toml can add MCP servers and hooks,
    # which would widen what a worker may do. Sign-in works without it.
    args = [executable, "exec", "--json", "--ignore-user-config", "--skip-git-repo-check", "-s", sandbox]
    if model != "default":
        args += ["-m", model]
    return args + ["-"]


def codex_env(base):
    """The environment minus what points claude at a provider. MM_WORKER_ACTIVE
    stays, so the worker can't dispatch."""
    return {name: value for name, value in base.items()
            if name not in SCRUBBED and not name.startswith("ANTHROPIC_")}


def _nested_error(message):
    """The API's whole error, which a codex error message often holds as JSON."""
    try:
        nested = json.loads(message)
    except ValueError:
        return None
    return nested if isinstance(nested, dict) else None


def _codex_failure(message):
    """(readable text, status) of a codex error message."""
    nested = _nested_error(message)
    if nested is None:
        return message[:500], "codex"
    inner = nested.get("error") if isinstance(nested.get("error"), dict) else {}
    text = inner.get("message") if isinstance(inner.get("message"), str) else message
    status = nested.get("status")
    # Any other status: the task didn't run, so another provider may.
    return text[:500], status if _is_int(status) else "codex"


def finish_codex_stream(path):
    """Add the result line claude ends its stream with, as finish_opencode_stream
    does. A stream that just stops gets none, which reads as a failure."""
    answer, turn, failure = None, None, None
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
             "output_tokens": 0}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                item = event.get("item") if isinstance(event.get("item"), dict) else {}
                # An item of type error is only a warning, such as an ignored setting.
                if (kind == "item.completed" and item.get("type") == "agent_message"
                        and isinstance(item.get("text"), str)):
                    answer = item["text"]  # earlier messages are narration
                elif kind == "turn.completed":
                    # A turn that completes after an error has recovered from it.
                    turn, failure = kind, None
                    counts = event.get("usage") if isinstance(event.get("usage"), dict) else {}
                    tokens = {key: counts[key] if _is_int(counts.get(key)) else 0
                              for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                                          "output_tokens")}
                    usage["input_tokens"] += tokens["input_tokens"] - tokens["cached_input_tokens"]
                    usage["cache_read_input_tokens"] += tokens["cached_input_tokens"]
                    usage["cache_creation_input_tokens"] += tokens["cache_write_input_tokens"]
                    # This already includes the reasoning tokens.
                    usage["output_tokens"] += tokens["output_tokens"]
                elif kind == "turn.failed":
                    turn = kind
                    error = event.get("error") if isinstance(event.get("error"), dict) else {}
                    message = str(error.get("message") or "")
                    # The error event before it usually says the same, and may be the
                    # one that parses.
                    if failure is None or (message and _nested_error(message) is not None):
                        failure = message or "turn failed"
                elif kind == "error":
                    failure = str(event.get("message") or "unknown error")
    except OSError:
        return
    if failure is not None:
        text, status = _codex_failure(failure)
        result = {"type": "result", "is_error": True, "result": text, "usage": usage,
                  "api_error_status": status}
    elif turn == "turn.completed" and answer is not None:
        result = {"type": "result", "is_error": False, "result": answer, "usage": usage}
    else:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")


def http_probe(url, key, model, timeout):
    """(status or None, body text) of a one-token request."""
    data = json.dumps({"model": model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "1"}]}).encode()
    request = urllib.request.Request(url, data=data, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            body = error.read(65536).decode("utf-8", "replace")
        except OSError:
            body = ""
        return error.code, body
    except urllib.error.URLError as error:
        return None, str(error.reason)
    except (ValueError, OSError) as error:
        return None, str(error)
    except Exception as error:  # http.client raises its own kinds for a broken server
        return None, "%s: %s" % (type(error).__name__, error)


# ---------- results ----------

def last_line(path):
    """The final line of a file, read from the end because a stream can be large."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, 2)
            end = handle.tell()
            size = 65536
            while True:
                start = max(0, end - size)
                handle.seek(start)
                tail = handle.read(end - start).rstrip()
                cut = tail.rfind(b"\n")
                if cut >= 0 or start == 0:
                    return tail[cut + 1:].decode("utf-8", "replace")
                size *= 2
    except OSError:
        return ""


def compact(path):
    """Keep only the final line: the stream's tool output can be large, and the
    model's own words in it must not be read as a quota error."""
    line = last_line(path)
    if not os.path.exists(path):
        return
    temp = path + ".tail"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(line + "\n" if line else "")
    os.replace(temp, path)


def inspect(path):
    info = {"line": last_line(path), "result": False, "error": False, "status": "-",
            "in": 0, "out": 0}
    try:
        event = json.loads(info["line"])
    except ValueError:
        return info
    # A stream cut off midway ends in an ordinary event, which is never a success.
    if not isinstance(event, dict) or event.get("type") != "result":
        return info
    usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}

    def count(key):
        value = usage.get(key)
        return value if isinstance(value, int) else 0

    info["result"] = True
    info["error"] = bool(event.get("is_error"))
    info["text"] = event.get("result", "")
    info["in"] = count("input_tokens") + count("cache_read_input_tokens") + count(
        "cache_creation_input_tokens")
    info["out"] = count("output_tokens")
    status = event.get("api_error_status")
    text = str(event.get("result", ""))
    if status is None and info["error"] and text.startswith("API Error"):
        words = text.split()
        status = words[2] if len(words) > 2 else "api"
    info["status"] = "-" if status is None else str(status)
    return info


# ---------- processes ----------

def _exited(proc):
    """Whether a child has exited, without reaping it where the system allows, so
    its pid, and with it the process group id, can't be reused meanwhile."""
    if proc.returncode is not None:
        return True
    if hasattr(os, "waitid"):
        try:
            return os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except OSError:
            pass
    return proc.poll() is not None


def _killpg(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def end_group(proc, grace):
    """TERM a worker's whole process group, and KILL it after grace seconds."""
    pgid = proc.pid
    _killpg(pgid, signal.SIGTERM)
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None and not _group_alive(pgid):
            return
        time.sleep(0.05)
    _killpg(pgid, signal.SIGKILL)
    proc.wait()


def logical_cwd():
    """The working folder as the shell names it, so a symlinked home still reads as ~."""
    pwd = os.environ.get("PWD")
    try:
        if pwd and os.path.isabs(pwd) and os.path.samefile(pwd, "."):
            return pwd
    except OSError:
        pass
    return os.getcwd()


def home_relative(path):
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + "/"):
        return "~" + path[len(home):]
    return path


def _positive(text, name, cast=int):
    try:
        value = cast(text)
    except (TypeError, ValueError):
        raise UsageError("%s must be a number: %s" % (name, text))
    if value <= 0:
        raise UsageError("%s must be above 0: %s" % (name, text))
    return value


# ---------- arguments ----------

VALUE_FLAGS = {
    "-l": "level", "--level": "level",
    "-m": "mm_model", "--mm-model": "mm_model",
    "-q": "qw_model", "--qw-model": "qw_model",
    "-z": "zp_model", "--zp-model": "zp_model",
    "-c": "claude_model", "--claude-model": "claude_model",
    "--codex-model": "codex_model",
    "--cli": "cli",
    "-b": "backend", "-P": "backend", "--backend": "backend",
    "-t": "timeout", "--timeout": "timeout",
    "-n": "name", "--name": "name",
    "-f": "prompt_file",
    "-H": "host", "--host": "host",
    "-C": "rdir", "--rdir": "rdir",
    "--fetch": "fetch_id",
    "--kill": "kill_id",
}


class Options(object):
    def __init__(self):
        self.level = None
        self.mm_model = None
        self.qw_model = None
        self.zp_model = None
        self.claude_model = os.environ.get("MM_WORKER_CLAUDE_MODEL") or None
        self.codex_model = None
        self.cli = os.environ.get("MM_WORKER_CLI") or None
        self.backend = "auto"
        self.timeout = None
        self.name = None
        self.prompt = None
        self.prompt_file = None
        self.host = os.environ.get("MM_WORKER_HOST") or None
        self.rdir = os.environ.get("MM_WORKER_RDIR") or None
        self.fetch_id = None
        self.kill_id = None
        self.raw_json = False
        self.status = False
        self.help = False
        self.extra = []


def parse_args(argv):
    options = Options()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in VALUE_FLAGS:
            if i + 1 >= len(argv):
                raise UsageError("%s needs a value" % arg)
            value = argv[i + 1]
            field = VALUE_FLAGS[arg]
            if field == "backend" and value == "local":
                raise UsageError("the local model backend has been removed; use minimax, zhipu, "
                                 "claude, qwen or codex")
            setattr(options, field, value)
            i += 2
        elif arg == "--json":
            options.raw_json = True
            i += 1
        elif arg == "--status":
            options.status = True
            i += 1
        elif arg in ("-h", "--help"):
            options.help = True
            i += 1
        elif arg == "--":
            options.extra = argv[i + 1:]
            break
        else:
            options.prompt = arg
            i += 1
    if options.backend != "auto" and options.backend not in PROVIDERS:
        raise UsageError("unknown backend: %s" % options.backend)
    if options.cli and options.cli not in CLIS:
        raise UsageError("unknown cli: %s" % options.cli)
    if options.cli == "opencode" and options.host:
        raise UsageError("--cli opencode runs on this machine only, not with -H")
    if options.backend == "codex" and options.host:
        raise UsageError("codex runs on this machine only, not with -H")
    if options.timeout is not None:
        options.timeout = _positive(options.timeout, "timeout")
    if options.host and options.host.startswith("-"):
        raise UsageError("bad host: %s" % options.host)
    return options


# ---------- the dispatcher ----------

class Dispatcher(object):
    def __init__(self, options, rules):
        self.o = options
        self.rules = rules
        self.state = os.path.join(os.path.expanduser("~"), ".cache", "mm-worker")
        self.runs = os.path.join(self.state, "runs")
        self.slots = os.path.join(self.state, "slots")
        self.lock = os.path.join(self.state, "select.lock")
        self.ledger_path = os.path.join(self.state, "ledger.tsv")
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-%d" % os.getpid()
        self.level = None
        self.timeout = options.timeout or rules["defaults"]["timeoutSeconds"]
        self.cooldown = self._env_int("MM_WORKER_COOLDOWN", rules["defaults"]["cooldownSeconds"], 0)
        self.poll = self._env_float("MM_WORKER_POLL", POLL_DEFAULT)
        self.offline_wait = self._env_float("MM_WORKER_OFFLINE_WAIT", OFFLINE_WAIT_DEFAULT)
        self.claude = None
        self.opencode = None
        self.cli = "claude"
        self.codex_refusal = None  # why codex can't take this task's arguments
        self.codex_skip_logged = False
        self.permission = None
        self.slot = None
        self.lock_held = False
        self.child = None
        self.child_is_worker = False
        self.rjob = None
        self.attempt = None
        self.defer = False
        self.pending = None
        self.base_env = dict(os.environ)
        self.seen_remote_logs = set()

    # --- paths and small helpers

    def run_file(self, suffix, run_id=None):
        return os.path.join(self.runs, (run_id or self.run_id) + suffix)

    @property
    def prompt_path(self):
        return self.run_file(".prompt.md")

    @property
    def stderr_path(self):
        return self.run_file(".stderr")

    @staticmethod
    def _env_int(name, default, minimum):
        text = os.environ.get(name)
        if not text:
            return default
        try:
            value = int(text)
        except ValueError:
            value = minimum - 1
        if value < minimum:
            log("ignoring %s=%s" % (name, text))
            return default
        return value

    @staticmethod
    def _env_float(name, default):
        text = os.environ.get(name)
        if not text:
            return default
        try:
            return max(0.0, float(text))
        except ValueError:
            log("ignoring %s=%s" % (name, text))
            return default

    def limit(self, provider):
        entry = self.rules["providers"].get(provider) or DEFAULT_RULES["providers"][provider]
        return self._env_int("MM_WORKER_SLOTS_" + provider.upper(), entry["slots"], 1)

    def where(self):
        return "@" + self.o.host if self.o.host else ""

    # --- signals

    def on_signal(self, signum, frame):
        if self.defer:
            self.pending = signum
            return
        raise Interrupted(signum)

    def critical(self, on):
        """While on, a signal waits, so a child or a slot is never half recorded."""
        self.defer = on
        if not on and self.pending is not None:
            signum, self.pending = self.pending, None
            raise Interrupted(signum)

    def interrupted(self):
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        self.stop_child()
        self.release_slot()
        self.release_lock()
        if self.rjob:
            log("interrupted here; the remote task keeps running. Fetch it with "
                "mm-worker --fetch %s, or cancel it with mm-worker --kill %s"
                % (self.run_id, self.run_id))
        elif self.attempt:
            label, model, started = self.attempt
            status = "cancelled" if self.cancelled() else "interrupted"
            self.write_ledger(label, model, status, started, None)
        return 130

    def stop_child(self):
        child, self.child = self.child, None
        if child is None:
            return
        if self.child_is_worker:
            end_group(child, KILL_GRACE)
            return
        try:
            child.terminate()
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        except OSError:
            pass

    # --- slots and the selection lock

    def reap(self, provider):
        folder = os.path.join(self.slots, provider)
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        live = []
        for name in names:
            slot = os.path.join(folder, name)
            if not os.path.isdir(slot):
                continue
            try:
                with open(os.path.join(slot, "pid")) as handle:
                    pid = handle.read().strip()
            except OSError:
                pid = ""
            if pid:
                if _pid_alive(pid):
                    live.append(name)
                    continue
            else:
                try:
                    if time.time() - os.stat(slot).st_mtime < SLOT_GRACE:
                        live.append(name)
                        continue
                except OSError:
                    continue
            shutil.rmtree(slot, ignore_errors=True)
        return live

    def used(self, provider):
        return len(self.reap(provider))

    def take_slot(self, provider):
        self.reap(provider)
        for index in range(1, self.limit(provider) + 1):
            folder = os.path.join(self.slots, provider, str(index))
            self.critical(True)
            try:
                os.mkdir(folder)
            except OSError:
                self.critical(False)
                continue
            self.slot = folder
            self.critical(False)
            with open(os.path.join(folder, "pid"), "w") as handle:
                handle.write("%d\n" % os.getpid())
            return True
        return False

    def release_slot(self):
        if self.slot:
            shutil.rmtree(self.slot, ignore_errors=True)
            self.slot = None

    def take_lock(self):
        while True:
            self.critical(True)
            try:
                os.mkdir(self.lock)
                self.lock_held = True
                self.critical(False)
                return
            except FileExistsError:
                self.critical(False)
            except OSError:
                self.critical(False)
                raise
            try:
                if time.time() - os.stat(self.lock).st_mtime > LOCK_STALE:
                    shutil.rmtree(self.lock, ignore_errors=True)
                    continue
            except OSError:
                continue
            time.sleep(0.2)

    def release_lock(self):
        if self.lock_held:
            shutil.rmtree(self.lock, ignore_errors=True)
            self.lock_held = False

    # --- cooling and probes

    def cooling(self, provider):
        try:
            with open(os.path.join(self.state, provider + "_unavailable_until")) as handle:
                return int(handle.read().strip() or 0) > time.time()
        except (OSError, ValueError):
            return False

    def cooling_reason(self, provider):
        try:
            with open(os.path.join(self.state, provider + "_unavailable_reason")) as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def mark_unavailable(self, provider, seconds, reason):
        """Seconds of None means the cooldown in the rules of the machine doing the work."""
        if self.o.host:
            self.remote_call("mark", provider=provider, seconds=seconds, reason=reason)
            return
        seconds = self.cooldown if seconds is None else seconds
        with open(os.path.join(self.state, provider + "_unavailable_until"), "w") as handle:
            handle.write("%d\n" % (int(time.time()) + seconds))
        with open(os.path.join(self.state, provider + "_unavailable_reason"), "w") as handle:
            handle.write(reason + "\n")
        log("%s marked unavailable for %d minutes: %s" % (provider, seconds // 60, reason))

    def probe(self, provider):
        """A one-token request, telling spent quota from a bad key or a network fault."""
        if provider == "claude":
            return True
        if self.o.host:
            answer = self.remote_call("probe", provider=provider)
            return bool(answer and answer.get("available"))
        if self.cooling(provider):
            log("%s is cooling down (%s), skipping" % (provider, self.cooling_reason(provider)))
            return False
        if provider == "codex":
            return True  # no key of ours to try: codex signs in on its own
        host_var, key_var = KEY_VARS[provider]
        values = dict(os.environ)
        values.update(read_env_file(env_file(provider)))
        key = values.get(key_var, "")
        path, model, timeout = PROBES[provider]
        code, body = http_probe(values.get(host_var, "") + path, key, model, timeout)
        if key:
            body = body.replace(key, "***")
        if code == 200 and not PROBE_ERROR_RE.search(body):
            try:
                os.unlink(os.path.join(self.state, provider + "_unavailable_until"))
            except OSError:
                pass
            return True
        shown = "none" if code is None else str(code)
        if code == 402 or QUOTA_RE.search(body):
            self.mark_unavailable(provider, None, "quota used up (HTTP %s)" % shown)
        elif code == 429:
            self.mark_unavailable(provider, 120, "rate limited (HTTP 429), maybe too many parallel runs")
        elif code in (401, 403):
            self.mark_unavailable(provider, 600, "authentication failed (HTTP %s), check ~/.config/%s/env"
                                  % (shown, provider))
        else:
            self.mark_unavailable(provider, 300, "probe failed (HTTP %s): %s"
                                  % (shown, " ".join(body.split())[:120]))
        return False

    # --- choosing a provider

    def candidates(self):
        if self.o.backend != "auto":
            return [self.o.backend]
        order = self.rules["order"]
        allowed = self.rules["levels"][self.level]
        # No key here means nothing to try. Probing anyway would only take a slot,
        # fail, and leave a cooldown that reads like a fault.
        names = [name for name in order
                 if self.rules["providers"][name]["enabled"] and name in allowed and has_key(name)]
        if "codex" in names and self.codex_refusal:
            if not self.codex_skip_logged:
                log("%s, so codex is skipped" % self.codex_refusal)
                self.codex_skip_logged = True
            names.remove("codex")
        if self.rules["balance"] == "priority":
            return names
        return sorted(names, key=lambda name: (Fraction(self.used(name), self.limit(name)),
                                               order.index(name)))

    def model_for(self, provider):
        explicit = {"minimax": self.o.mm_model, "zhipu": self.o.zp_model,
                    "qwen": self.o.qw_model, "claude": self.o.claude_model,
                    "codex": self.o.codex_model}[provider]
        if explicit:
            return explicit
        return (self.rules["levels"][self.level].get(provider)
                or LEVEL_FALLBACK[self.level][provider])

    def cancelled(self):
        return os.path.exists(self.run_file(".cancelled"))

    # --- output and the ledger

    def write_ledger(self, label, model, status, started, info):
        info = info or {"in": 0, "out": 0}
        try:
            with open(self.prompt_path, "rb") as handle:
                head = handle.read(60)
        except OSError:
            head = b""
        head = head.replace(b"\n", b" ").replace(b"\t", b" ")
        fields = [time.strftime("%Y-%m-%d %H:%M:%S"), label, model, status,
                  "%ds" % max(0, int(time.time() - started)), "in=%d" % info["in"],
                  "out=%d" % info["out"], home_relative(logical_cwd())]
        line = b"\t".join(os.fsencode(field) for field in fields) + b"\t" + head + b"\n"
        with open(self.ledger_path, "ab") as handle:
            handle.write(line)

    def show(self, info, stream):
        """Print a result event: to stdout for the final answer, stderr otherwise."""
        if not info["result"]:
            return
        text = info["line"] if self.o.raw_json else info.get("text", "")
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)
        try:
            stream.write(text + "\n")
            stream.flush()
        except BrokenPipeError:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, stream.fileno())
        except (OSError, ValueError):
            pass

    # --- running locally

    def run_local(self, provider, alias, out):
        # Whatever the rule cli says: codex is a provider with its own command.
        streamed = provider == "codex" or self.cli == "opencode"
        if provider == "codex":
            model = self.model_for(provider)
            sandbox, notes = codex_sandbox(self.o.extra)  # prepare checked the arguments
            for note in notes:
                log(note)
            executable = find_codex()
            if not executable:
                log("codex not found on PATH or in %s" % ", ".join(CODEX_FALLBACKS))
                return 127
            args = codex_args(executable, sandbox, model)
            env = codex_env(self.base_env)
            # Fleet reads the model from this line: codex's events don't carry one.
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "system", "subtype": "init",
                                         "model": "codex" if model == "default" else model}) + "\n")
        elif self.cli == "opencode":
            model = self.model_for(provider)
            args = opencode_args(self.opencode, provider, model)
            env = opencode_env(provider, model, self.permission, self.base_env)
            # Fleet reads the model from this line: opencode's events don't carry one.
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "system", "subtype": "init",
                                         "model": MODEL_NAMES[provider].get(model, model)}) + "\n")
        else:
            args = [self.claude, "--model", alias, "-p", "--output-format", "stream-json",
                    "--verbose"] + self.o.extra
            env = provider_env(provider, self.base_env)
        with open(self.prompt_path, "rb") as stdin, open(out, "ab" if streamed else "wb") as stdout, \
                open(self.stderr_path, "ab") as stderr:
            self.critical(True)
            try:
                # Its own session, so the whole tree can be stopped together and a
                # terminal's Ctrl-C reaches only the dispatcher, which then stops it.
                self.child = subprocess.Popen(args, stdin=stdin, stdout=stdout, stderr=stderr,
                                              env=env, start_new_session=True, close_fds=True)
                self.child_is_worker = True
            finally:
                self.critical(False)
        proc = self.child
        deadline = time.time() + self.timeout
        timed_out = False
        while not _exited(proc):
            if time.time() >= deadline:
                timed_out = True
                log("timed out after %ds, stopping the worker" % self.timeout)
                end_group(proc, KILL_GRACE)
                break
            time.sleep(0.1)
        # Whatever the worker left running in its group goes with it.
        end_group(proc, STRAGGLER_GRACE)
        self.child = None
        if provider == "codex":
            finish_codex_stream(out)
        elif self.cli == "opencode":
            finish_opencode_stream(out)
        compact(out)
        if self.cancelled():
            return CANCELLED
        return 124 if timed_out else proc.returncode

    # --- running on another machine

    def ssh(self, command, stdin=None, data=None, capture=False):
        """(exit code, output) of one ssh call; 255 means it couldn't connect."""
        args = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3", self.o.host, command]
        try:
            stderr = open(self.stderr_path, "ab") if os.path.isdir(self.runs) else subprocess.DEVNULL
        except OSError:
            stderr = subprocess.DEVNULL
        try:
            self.critical(True)
            try:
                self.child = subprocess.Popen(
                    args, stdin=subprocess.PIPE if data is not None else (stdin or subprocess.DEVNULL),
                    stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=stderr)
                self.child_is_worker = False
            except OSError as error:
                log("cannot run ssh: %s" % error)
                return 255, b""
            finally:
                self.critical(False)
            output, _ = self.child.communicate(data)
            code = self.child.returncode
            self.child = None
            return code, output or b""
        finally:
            if stderr is not subprocess.DEVNULL:
                stderr.close()

    def ssh_retry(self, command):
        started, warned = time.time(), False
        while self.ssh(command)[0] != 0:
            if time.time() - started > self.offline_wait:
                return False
            if not warned:
                log("cannot reach %s, retrying every %ds (the remote task is not affected)"
                    % (self.o.host, RETRY_SLEEP))
                warned = True
            time.sleep(RETRY_SLEEP)
        return True

    # The files --remote works from. Queued tasks ask again every few seconds, so
    # these travel once rather than with each request.
    @property
    def stage(self):
        return ".cache/mm-worker/queued/" + self.run_id

    def stage_file(self, path, name):
        started = time.time()
        command = "mkdir -p %s && cat > %s/%s" % (self.stage, self.stage, name)
        while True:
            with open(path, "rb") as source:
                if self.ssh(command, stdin=source)[0] == 0:
                    return True
            if time.time() - started > SUBMIT_GIVE_UP:
                log("could not reach %s for %d minutes, giving up" % (self.o.host, SUBMIT_GIVE_UP // 60))
                return False
            time.sleep(RETRY_SLEEP)

    def stage_task(self):
        files = [(os.path.abspath(__file__), "mm-worker.py"), (self.prompt_path, "prompt.md")]
        if self.o.name:
            files.append((self.run_file(".name"), "name"))
        return all(self.stage_file(path, name) for path, name in files)

    def remote_call(self, op, **fields):
        """The target's answer to one request, or None if there is none to be had."""
        request = dict(fields, op=op, run_id=self.run_id)
        command = "python3 -I %s/mm-worker.py --remote %s" % (self.stage, shlex.quote(json.dumps(request)))
        started = time.time()
        while True:
            code, output = self.ssh(command, capture=True)
            if code != 255:
                break
            if time.time() - started > SUBMIT_GIVE_UP:
                log("could not reach %s for %d minutes, giving up" % (self.o.host, SUBMIT_GIVE_UP // 60))
                return None
            time.sleep(RETRY_SLEEP)
        try:
            answer = json.loads(output.decode("utf-8", "replace").strip().splitlines()[-1])
        except (ValueError, IndexError):
            answer = None
        if not isinstance(answer, dict) or code != 0:
            log("%s gave no usable answer (exit %d); see %s" % (self.o.host, code, self.stderr_path))
            return None
        for line in answer.get("log", ()):
            if line not in self.seen_remote_logs:
                self.seen_remote_logs.add(line)
                log("%s: %s" % (self.o.host, line[len("[mm-worker] "):]))
        if answer.get("state") == "error":
            log("%s could not take the task: %s" % (self.o.host, answer.get("error")))
            return None
        return answer

    def remote_loop(self):
        o = self.o
        tried, last_wait_log = [], 0.0
        while True:
            answer = self.remote_call(
                "start", tried=tried, level=self.level, backend=o.backend, timeout=o.timeout, rdir=o.rdir,
                extra=o.extra, name=bool(o.name),
                models={"mm_model": o.mm_model, "zp_model": o.zp_model, "qw_model": o.qw_model,
                        "claude_model": o.claude_model})
            if answer is None:
                return 1
            state = answer["state"]
            tried = answer.get("tried", tried)
            if state == "none":
                log("no enabled provider on %s takes %s tasks" % (o.host, self.level))
                return 1
            if state == "unavailable":
                log("all providers on %s are unavailable" % o.host)
                return 1
            if state == "retry":
                continue
            if state == "busy":
                if time.time() - last_wait_log >= 60:
                    log("all slots on %s are busy (%s), waiting" % (o.host, " ".join(answer["candidates"])))
                    last_wait_log = time.time()
                time.sleep(QUEUE_SLEEP)
                continue
            provider = answer["provider"]
            rc = self.run_on(provider, answer)
            if self.cancelled():
                log("the task was cancelled; not trying another provider")
                return 130
            if rc != FAILOVER:
                return rc
            tried = tried + [provider]
            log("%s failed, trying the next provider" % provider)
            self.add_failover_note()
            if o.backend != "auto":
                return rc
            if not self.stage_file(self.prompt_path, "prompt.md"):
                return 1

    def dispatch_remote(self):
        self.prepare()
        if not self.stage_task():
            return 1
        rc = self.remote_loop()
        self.ssh("rm -rf %s" % self.stage)
        return rc

    # --- the other machine's side: this file running there, with --remote

    def remote_answer(self, request, stage):
        op = request["op"]
        provider = request.get("provider")
        if provider is not None and provider not in PROVIDERS:
            raise ValueError("unknown provider")
        if op == "probe":
            return {"state": "done", "available": self.probe(provider)}
        if op == "mark":
            self.mark_unavailable(provider, request["seconds"], request["reason"])
            return {"state": "done"}
        if op == "start":
            return self.remote_start(request, stage)
        raise ValueError("unknown request")

    def job_name(self, provider):
        return ".cache/mm-worker/jobs/%s-%s" % (self.run_id, provider)

    def remote_start(self, request, stage):
        tried = request["tried"]
        self.sweep_queued()
        earlier = self.earlier_start(tried)
        if earlier:
            return earlier
        got = None
        # The same selection as on this machine, so its slots, order and levels all count.
        # Never codex: it runs only on the machine that dispatches it.
        self.take_lock()
        try:
            candidates = [name for name in self.candidates() if name != "codex"]
            for provider in candidates:
                if provider in tried:
                    continue
                if self.cooling(provider):
                    tried = tried + [provider]
                    continue
                if self.take_slot(provider):
                    got = provider
                    break
        finally:
            self.release_lock()
        if not candidates:
            return {"state": "none", "tried": tried}
        if not got:
            state = "busy" if [name for name in candidates if name not in tried] else "unavailable"
            return {"state": state, "candidates": candidates, "tried": tried}
        if not self.probe(got):
            self.release_slot()
            return {"state": "retry", "tried": tried + [got]}
        return self.remote_launch(got, stage, tried)

    def earlier_start(self, tried):
        """A retry after a dropped connection must not start the task twice."""
        prefix = self.run_id + "-"
        jobs = os.path.join(self.state, "jobs")
        for name in os.listdir(jobs):
            if not name.startswith(prefix) or name[len(prefix):] in tried:
                continue
            folder = os.path.join(jobs, name)
            try:
                with open(os.path.join(folder, "claim.json")) as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                pass
            if time.time() - os.stat(folder).st_mtime < UNFINISHED_START:
                return {"state": "busy", "candidates": [name[len(prefix):]], "tried": tried}
            shutil.rmtree(folder, ignore_errors=True)
        return None

    def sweep_queued(self):
        root = os.path.join(self.state, "queued")
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            try:
                if time.time() - os.stat(folder).st_mtime > QUEUED_KEEP:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass

    def remote_launch(self, provider, stage, tried):
        rjob = self.job_name(provider)
        try:
            os.mkdir(rjob)
        except FileExistsError:
            self.release_slot()
            return {"state": "busy", "candidates": [provider], "tried": tried}
        shutil.copyfile(os.path.join(stage, "prompt.md"), os.path.join(rjob, "prompt.md"))
        if self.o.name:
            shutil.copyfile(os.path.join(stage, "name"), os.path.join(rjob, "name"))
        model = self.model_for(provider)
        alias = ALIASES[provider].get(model, model)
        command = [".local/bin/mm-remote-run", provider, str(self.timeout), self.o.rdir,
                   "--model", alias, "-p", "--output-format", "stream-json", "--verbose"] + self.o.extra
        job = shlex.quote(rjob)
        # While running, out.json is the event stream Fleet follows; afterwards
        # only the final result line stays, since the stream can be large.
        inner = ("echo $$ > {j}/pid; {cmd} < {j}/prompt.md > {j}/out.json 2> {j}/stderr; c=$?; "
                 "tail -n 1 {j}/out.json > {j}/out.tmp && mv {j}/out.tmp {j}/out.json; "
                 "echo $c > {j}/rc; rm -rf {slot}").format(
                     j=job, cmd=" ".join(shlex.quote(a) for a in command), slot=shlex.quote(self.slot))
        claim = {"state": "started", "provider": provider, "model": model, "timeout": self.timeout,
                 "rjob": rjob}
        # Nothing may stop this between starting the job and handing it the slot.
        self.critical(True)
        try:
            # A session of its own, so it outlives ssh and this machine's dispatcher.
            child = subprocess.Popen(["bash", "-c", inner], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            open(os.path.join(rjob, "started"), "w").close()
            with open(os.path.join(rjob, "claim.tmp"), "w") as handle:
                json.dump(claim, handle)
            os.replace(os.path.join(rjob, "claim.tmp"), os.path.join(rjob, "claim.json"))
            # From here the job holds the slot, for as long as it runs. Fleet reads
            # this name to tell it from a worker dispatched on this machine.
            with open(os.path.join(self.slot, "job"), "w") as handle:
                handle.write(os.path.basename(rjob) + "\n")
            with open(os.path.join(self.slot, "pid.tmp"), "w") as handle:
                handle.write("%d\n" % child.pid)
            os.replace(os.path.join(self.slot, "pid.tmp"), os.path.join(self.slot, "pid"))
            self.slot = None
        finally:
            self.critical(False)
        return claim

    def remote_wait(self, out, run_id):
        """The remote exit code (a timeout's 137 as 124), or CANCELLED."""
        job = shlex.quote(self.rjob)
        last_ok, offline = time.time(), False
        while True:
            code, text = self.ssh("cat %s/rc 2>/dev/null || echo running" % job, capture=True)
            if code == 0:
                if offline:
                    log("connection to %s is back" % self.o.host)
                offline, last_ok = False, time.time()
                rc = text.decode("utf-8", "replace").strip()
                if rc and rc != "running":  # empty: rc is being written
                    break
            else:
                if not offline:
                    log("cannot reach %s; the task keeps running there, still waiting" % self.o.host)
                    offline = True
                if time.time() - last_ok > self.offline_wait:
                    log("could not reach %s for %ds; once it is back, fetch the result with "
                        "mm-worker --fetch %s" % (self.o.host, self.offline_wait, run_id))
                    raise Exit(1)
            time.sleep(self.poll)
        started = time.time()
        # tail: if the stream was never compacted (a cancelled task), still only one line.
        while True:
            code, text = self.ssh("tail -n 1 %s/out.json" % job, capture=True)
            if code == 0:
                with open(out, "wb") as handle:
                    handle.write(text)
                break
            if time.time() - started > self.offline_wait:
                raise Exit(1)
            time.sleep(RETRY_SLEEP)
        code, text = self.ssh("cat %s/stderr" % job, capture=True)
        if code == 0 and text:
            with open(self.run_file(".stderr", run_id), "ab") as handle:
                handle.write(text)
        self.rjob = None
        if rc == CANCELLED:
            return CANCELLED
        try:
            number = int(rc)
        except ValueError:
            number = 1
        return 124 if number == 137 else number

    # --- one attempt

    def run_on(self, provider, claim=None):
        started = time.time()
        model = claim["model"] if claim else self.model_for(provider)
        alias = ALIASES[provider].get(model, model)
        out = self.run_file(".%s.json" % provider)
        label = provider + self.where()
        self.attempt = (label, model, started)
        log("backend=%s (%s) level=%s%s" % (provider, model, self.level,
                                            " host=" + self.o.host if self.o.host else ""))
        if claim:
            self.timeout = claim["timeout"]
            self.rjob = claim["rjob"]
            with open(self.run_file(".remote"), "w") as handle:
                handle.write("%s %s %s\n" % (self.o.host, self.rjob, provider))
            log("submitted to %s (%s); losing the connection won't stop it. Fetch it later with "
                "mm-worker --fetch %s" % (self.o.host, self.rjob, self.run_id))
            rc = self.remote_wait(out, self.run_id)
        else:
            rc = self.run_local(provider, alias, out)
        self.attempt = None
        if rc == CANCELLED:
            self.write_ledger(label, model, "cancelled", started, None)
            log("the task was cancelled; not trying another provider")
            raise Exit(130)
        info = inspect(out)
        if provider == "claude":
            if rc == 0 and not info["result"]:
                rc = 1
            status = "ok" if rc == 0 else "timeout" if rc == 124 else "fail(%d)" % rc
            self.write_ledger(label, model, status, started, info)
            self.show(info, sys.stdout)
            return rc
        if rc == 124:
            self.write_ledger(label, model, "timeout", started, info)
            # Spent quota can look like a hang, as the CLI retries forever.
            if self.probe(provider):
                log("%s timed out after %ds with quota left: the task is too big, split it"
                    % (provider, self.timeout))
                self.show(info, sys.stdout)
                return 124
            self.show(info, sys.stderr)
            return FAILOVER
        if not info["result"] or (info["error"] and info["status"] != "-"):
            self.write_ledger(label, model, "api_error(%s)" % info["status"], started, info)
            # stderr: stdout is only for the final answer, and another provider gets the task.
            self.show(info, sys.stderr)
            if info["result"] and (info["status"] == "402" or QUOTA_RE.search(info["line"])):
                self.mark_unavailable(provider, None,
                                      "quota used up during the run (status %s)" % info["status"])
            elif info["status"] == "429":
                self.mark_unavailable(provider, 120, "rate limited during the run (429)")
            return FAILOVER
        self.write_ledger(label, model, "error" if info["error"] else "ok", started, info)
        self.show(info, sys.stdout)
        return 1 if info["error"] else 0

    # --- the whole task

    def prepare(self):
        o = self.o
        if o.host:
            if not o.rdir:
                cwd, home = logical_cwd(), os.path.expanduser("~")
                o.rdir = cwd[len(home) + 1:] if cwd.startswith(home + "/") else cwd
            check = ("test -x .local/bin/mm-remote-run && test -d %s && command -v python3 > /dev/null"
                     % shlex.quote(o.rdir))
            if self.ssh(check)[0] != 0:
                log("%s is unreachable, or lacks python3, mm-remote-run or the folder %s" % (o.host, o.rdir))
                raise Exit(2)
        self.level = o.level or ("simple" if o.mm_model == "fast" else "normal")
        if self.level not in LEVELS:
            log("unknown level: %s" % self.level)
            raise Exit(2)
        if self.level != "extreme" and o.claude_model and "fable" in o.claude_model:
            fallback = "claude-opus-5-5" if self.level == "hard" else "sonnet"
            log("fable is only for extreme tasks, using %s" % fallback)
            o.claude_model = fallback
        if self.level not in ("hard", "extreme") and o.claude_model and "opus" in o.claude_model:
            log("opus is only for hard and extreme tasks, using sonnet")
            o.claude_model = "sonnet"
        # A task sent to another machine runs there with mm-remote-run, under claude.
        self.cli = "claude" if o.host else (o.cli or self.rules.get("cli", "claude"))
        if not o.host:
            try:
                codex_sandbox(o.extra)
            except UsageError as error:
                self.codex_refusal = str(error)
        # Asked for by -b, codex needs neither claude nor opencode.
        if o.backend == "codex":
            if self.codex_refusal:
                log(self.codex_refusal)
                raise Exit(2)
            if not find_codex():
                log("codex not found on PATH or in %s" % ", ".join(CODEX_FALLBACKS))
                raise Exit(127)
        elif self.cli == "opencode":
            if o.backend == "claude":
                log("opencode runs minimax, zhipu or qwen; claude needs --cli claude")
                raise Exit(2)
            try:
                self.permission = opencode_permission(o.extra)
            except UsageError as error:
                log(str(error))
                raise Exit(2)
            self.opencode = find_opencode()
            if not self.opencode:
                log("opencode not found on PATH or in %s" % ", ".join(OPENCODE_FALLBACKS))
                raise Exit(127)
        elif not o.host:
            self.claude = find_claude()
            if not self.claude:
                log("claude not found on PATH or in %s" % ", ".join(CLAUDE_FALLBACKS))
                raise Exit(127)
        path = self.prompt_path
        try:
            if o.prompt_file:
                with open(o.prompt_file, "rb") as source:
                    data = source.read()
            elif o.prompt:
                data = os.fsencode(o.prompt) + b"\n"
            elif sys.stdin is not None and not sys.stdin.isatty():
                data = sys.stdin.buffer.read()
            else:
                log("no prompt given")
                raise Exit(2)
        except OSError as error:
            log("cannot read the prompt: %s" % error.strerror)
            raise Exit(2)
        if not data.strip():
            log("the prompt is empty")
            raise Exit(2)
        with open(path, "wb") as handle:
            handle.write(data)
        if o.name:
            with open(self.run_file(".name"), "wb") as handle:
                handle.write(os.fsencode(o.name) + b"\n")

    def add_failover_note(self):
        with open(self.prompt_path, "rb") as handle:
            prompt = handle.read()
        note = FAILOVER_NOTE.encode()
        if not prompt.startswith(note):
            with open(self.prompt_path + ".tmp", "wb") as handle:
                handle.write(note + prompt)
            os.replace(self.prompt_path + ".tmp", self.prompt_path)

    def dispatch(self):
        if self.o.host:
            return self.dispatch_remote()
        self.prepare()
        tried, probed, last_wait_log = [], set(), 0.0
        while True:
            got = None
            # Choosing and taking a slot under one lock: otherwise a batch started
            # together sees the same counts and piles onto one provider.
            self.take_lock()
            try:
                candidates = [name for name in self.candidates()
                              if self.cli != "opencode" or name in OPENCODE_BACKENDS or name == "codex"]
                for provider in candidates:
                    if provider in tried:
                        continue
                    if self.cooling(provider):
                        tried.append(provider)
                        continue
                    if self.take_slot(provider):
                        got = provider
                        break
            finally:
                self.release_lock()
            if not candidates:
                log("no enabled provider takes %s tasks" % self.level)
                return 1
            # Probing happens outside the lock, so it doesn't hold up other dispatchers.
            if got and got not in probed:
                if self.probe(got):
                    probed.add(got)
                else:
                    self.release_slot()
                    tried.append(got)
                    continue
            if not got:
                if not [name for name in candidates if name not in tried]:
                    log("all providers are unavailable")
                    return 1
                if time.time() - last_wait_log >= 60:
                    log("all slots are busy (%s), waiting" % " ".join(candidates))
                    last_wait_log = time.time()
                time.sleep(QUEUE_SLEEP)
                continue
            rc = self.run_on(got)
            self.release_slot()
            # Fleet's cancel leaves this marker: the user wants it stopped, not retried.
            if self.cancelled():
                log("the task was cancelled; not trying another provider")
                return 130
            if rc != FAILOVER:
                return rc
            tried.append(got)
            log("%s failed, trying the next provider" % got)
            self.add_failover_note()
            if self.o.backend != "auto":
                return rc

    def fetch_or_kill(self):
        run_id = self.o.fetch_id or self.o.kill_id
        record = self.run_file(".remote", run_id) if RUN_ID_RE.match(run_id) else None
        try:
            with open(record) as handle:
                host, rjob, provider = handle.read().split()[:3]
        except (TypeError, OSError, ValueError):
            log("no remote task recorded as %s" % run_id)
            return 2
        if host.startswith("-") or provider not in PROVIDERS:
            log("bad remote task record: %s" % record)
            return 2
        # Logs, stderr and the ledger then all refer to the run being fetched.
        self.run_id = run_id
        self.o.host, self.rjob = host, rjob
        job = shlex.quote(rjob)
        if self.o.kill_id:
            command = ("p=$(cat {j}/pid); [ ! -e {j}/rc ] && echo cancelled > {j}/rc && "
                       "pkill -TERM -s $p; true; grep -qx cancelled {j}/rc").format(j=job)
            self.rjob = None
            if self.ssh(command)[0] == 0:
                log("cancelled %s (%s)" % (run_id, host))
                return 0
            log("%s has already finished, or %s cannot be reached" % (run_id, host))
            return 1
        out = self.run_file(".%s.json" % provider, run_id)
        try:
            started = os.stat(self.run_file(".prompt.md", run_id)).st_mtime
        except OSError:
            started = time.time()
        rc = self.remote_wait(out, run_id)
        if rc == CANCELLED:
            log("the remote task was cancelled")
            return 130
        info = inspect(out)
        self.write_ledger("%s@%s" % (provider, host), "fetched", "fetched(%d)" % rc, started, info)
        self.show(info, sys.stdout)
        return rc


def _pid_alive(text):
    try:
        pid = int(text)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def show_status(rules):
    dispatcher = Dispatcher(Options(), rules)
    order = rules["order"] + [name for name in PROVIDERS if name not in rules["order"]]
    auto = [name for name in rules["order"] if rules["providers"][name]["enabled"]]
    manual = [name for name in order if name not in auto]
    log("automatic dispatch uses: %s%s" % (" ".join(auto) or "nothing",
                                          " (only with -b: %s)" % " ".join(manual) if manual else ""))
    now = time.time()
    for name in order:
        if name == "claude":
            continue
        if dispatcher.cooling(name):
            try:
                with open(os.path.join(dispatcher.state, name + "_unavailable_until")) as handle:
                    until = int(handle.read().strip())
            except (OSError, ValueError):
                until = int(now)
            log("%s unavailable, %d minutes left (%s)"
                % (name, (until - now) // 60, dispatcher.cooling_reason(name)))
        else:
            log("%s available (not cooling down)" % name)
    log("slots in use: " + "  ".join("%s %d/%d" % (name, dispatcher.used(name), dispatcher.limit(name))
                                     for name in order))
    try:
        with open(dispatcher.ledger_path, "rb") as handle:
            lines = handle.read().decode("utf-8", "replace").splitlines()[-15:]
    except OSError:
        return 0
    rows = [line.split("\t") for line in lines if line]
    widths = {}
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths.get(index, 0), len(cell))
    sys.stdout.write("Last 15 runs:\n")
    for row in rows:
        cells = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
        sys.stdout.write("  ".join(cells).rstrip() + "\n")
    sys.stdout.flush()
    return 0


def remote_main(argument):
    """One request from a dispatcher on another machine, answered with one JSON line."""
    captured = sys.stderr = io.StringIO()  # logs go back in the answer
    dispatcher = None
    try:
        request = json.loads(argument)
        home = os.path.expanduser("~")
        os.chdir(home)
        state = os.path.join(home, ".cache", "mm-worker")
        for folder in ["jobs", "queued"] + [os.path.join("slots", name) for name in PROVIDERS]:
            os.makedirs(os.path.join(state, folder), exist_ok=True)
        rules_path = os.path.join(home, ".config", "mm-worker", "rules.json")
        rules, problem = load_rules(rules_path)
        if problem:
            log("ignoring %s (%s), using the default rules" % (home_relative(rules_path), problem))
        options = Options()
        options.host = None
        for field in ("level", "timeout", "rdir"):
            setattr(options, field, request.get(field))
        options.backend = request.get("backend", "auto")
        options.extra = request.get("extra", [])
        options.name = request.get("name")
        for field, value in request.get("models", {}).items():
            setattr(options, field, value)
        if not RUN_ID_RE.match(request["run_id"]):
            raise ValueError("bad run id")
        dispatcher = Dispatcher(options, rules)
        dispatcher.run_id = request["run_id"]
        dispatcher.level = options.level
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, dispatcher.on_signal)
        answer = dispatcher.remote_answer(request, os.path.dirname(os.path.abspath(__file__)))
    except Interrupted:
        return 1
    except Exception as error:  # the dispatcher reports it; a traceback here reaches nobody
        answer = {"state": "error", "error": "%s: %s" % (type(error).__name__, error)}
    finally:
        if dispatcher:
            dispatcher.release_slot()
            dispatcher.release_lock()
    answer["log"] = captured.getvalue().splitlines()
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()
    return 0


def main(argv):
    if argv[:1] == ["--remote"] and len(argv) == 2:
        return remote_main(argv[1])
    if os.environ.get("MM_WORKER_ACTIVE"):
        log("refused: already inside a worker. Nested dispatch is not allowed; do the task directly")
        return 3
    os.environ["MM_WORKER_ACTIVE"] = "1"
    for name in list(os.environ):
        if name in SCRUBBED or name.startswith("ANTHROPIC_DEFAULT_"):
            del os.environ[name]
    try:
        options = parse_args(argv)
    except UsageError as error:
        log(str(error))
        return 2
    if options.help:
        sys.stdout.write(USAGE)
        return 0
    home = os.path.expanduser("~")
    state = os.path.join(home, ".cache", "mm-worker")
    for folder in ["runs"] + [os.path.join("slots", name) for name in PROVIDERS]:
        os.makedirs(os.path.join(state, folder), exist_ok=True)
    rules_path = os.path.join(home, ".config", "mm-worker", "rules.json")
    rules, problem = load_rules(rules_path)
    if problem:
        log("ignoring %s (%s), using the default rules" % (home_relative(rules_path), problem))
    if options.status:
        return show_status(rules)
    dispatcher = Dispatcher(options, rules)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, dispatcher.on_signal)
    try:
        if options.fetch_id or options.kill_id:
            return dispatcher.fetch_or_kill()
        return dispatcher.dispatch()
    except Exit as done:
        return done.code
    except Interrupted:
        return dispatcher.interrupted()
    finally:
        dispatcher.release_slot()
        dispatcher.release_lock()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
