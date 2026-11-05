# Fleet: per-machine dispatch rules

The contract between `mm-worker.py` (runs on each machine), `fleet-agent.py`
(reports on it and edits its rules) and the Fleet window in TokNotch.

Each machine limits itself. Nothing coordinates machines at run time, so a
machine keeps working when the Mac is off. The app warns when the limits added
up across machines exceed what an account allows.

## Files on each machine

| Path | Owner | Purpose |
|---|---|---|
| `~/.local/bin/mm-worker` | app installs it | The dispatcher: a copy of `Sources/Resources/mm-worker.py` |
| `~/.config/mm-worker/rules.json` | app edits it | This machine's rules. Missing means the defaults below |
| `~/.config/{minimax,zhipu,qwen}/env` | the user | API keys. The app never reads, copies or shows these |
| `~/.cache/mm-worker/` | mm-worker | Slots, runs, jobs and ledger. Layout below |

## rules.json

```json
{
  "version": 1,
  "revision": 0,
  "balance": "proportional",
  "order": ["minimax", "zhipu", "claude"],
  "providers": {
    "minimax": {"enabled": true, "slots": 8},
    "zhipu": {"enabled": true, "slots": 4},
    "claude": {"enabled": true, "slots": 4},
    "qwen": {"enabled": false, "slots": 4}
  },
  "levels": {
    "simple": {"minimax": "fast", "zhipu": "flash", "claude": "sonnet"},
    "normal": {"minimax": "m3", "zhipu": "flash", "claude": "sonnet"},
    "hard": {"claude": "claude-opus-5-5"}
  },
  "defaults": {"timeoutSeconds": 900, "cooldownSeconds": 1800}
}
```

The JSON above is the default, used whenever the file is missing or invalid.
`mm-worker.py` and `fleet-agent.py` each hold a copy as `DEFAULT_RULES`, and a
test keeps them equal.

- `version`: always `1`. Any other value is invalid.
- `revision`: a counter. Each successful write adds 1, which lets a writer notice
  that it would overwrite someone else's change.
- `balance`: `proportional` picks the provider with the lowest `used / slots`.
  `priority` takes the first in `order` with a free slot. Ties in `proportional`
  also follow `order`.
- `order`: preference order. Every enabled provider must appear, once. A disabled
  provider may appear or not, which is why the defaults can leave out `qwen`.
  Nothing outside `providers` may appear.
- `providers.<p>.enabled`: a disabled provider gets no automatic work. `-b <p>`
  still reaches it, as a deliberate override.
- `providers.<p>.slots`: an integer from 1 to 32.
- `levels.<level>`: the providers that may take this level and the model each
  uses. A provider that isn't listed doesn't take the level. All three levels
  must exist and each must name at least one provider.
- `defaults.timeoutSeconds`: 30 to 86400. `defaults.cooldownSeconds`: 0 to 86400.

Allowed models, by provider:

| Provider | Models | Meaning |
|---|---|---|
| `minimax` | `m3`, `m27`, `fast` | MiniMax-M3, M2.7, M2.7-highspeed |
| `zhipu` | `flash` | GLM-5.3-Flash |
| `claude` | `sonnet`, `haiku`, `claude-opus-5-5` | |
| `qwen` | `glm`, `max`, `flash`, `lite`, `ds` | GLM-5.3, qwen3.8-max, qwen3.8-flash, qwen3.6-flash, DeepSeek-V4-Pro-0813 |

The optional top-level `cli` chooses the agent that runs workers: `claude` (what
leaving it out means) or `opencode`. It needs `MM_WORKER_VERSION` 3, and `rules-set`
refuses it on an older dispatcher or, for `opencode`, on a machine without the
command. `--cli` and `MM_WORKER_CLI` override it for one task.

With `opencode`:

- Each run carries its whole configuration in `OPENCODE_CONFIG_CONTENT`, so a machine
  needs only the command and the provider's env file. Keys stay in the environment.
- `claude` is never a candidate, as there is nothing to log in with, and `-b claude`
  is an error. Levels that name only `claude` have no provider.
- After `--`, only `--allowedTools` is accepted, and it is translated to permissions.
  Anything else stops the task, as dropping a flag could widen what a worker may do.
- Reading is open, except `.env` files. Everything else is denied unless named.
  `Bash(cmd:*)` also refuses redirection, command substitution, chaining and pipes,
  since opencode matches by prefix. It still can't limit what `cmd` itself does, so
  `Bash(python3:*)` is as open as running any program.
- Only on this machine: a task with `-H` runs there under `claude`.
- Tokens reach the ledger, and Fleet counts them from opencode's own database. See
  `docs/FLEET-USAGE.md`.

A rule may also name any model by its full name, as the provider's API lists it:
up to 80 letters, digits, `.`, `_`, `:` or `-`, starting with `MiniMax-`
(`minimax`), `glm-` (`zhipu`), `claude-` (`claude`), or `qwen`, `glm-` or
`deepseek-` (`qwen`), in any case. The prefix keeps one provider's model off another
and keeps a pasted key from passing as a model. It needs `MM_WORKER_VERSION` 2, as
an older dispatcher reads such a file as invalid. `mm-worker` passes the name to
`claude --model` unchanged.

Unknown keys are invalid, so a typo can't silently do nothing. An invalid file
makes `mm-worker` warn on stderr and use the defaults. It never stops work.

Rules apply to tasks started afterwards. Running workers are not touched.

With `-H`, the target machine's rules apply, the same as for a task started on it:
its slots, `balance`, `order`, `enabled`, `levels` and `defaults`, its keys and its
cooling. The dispatching machine's rules play no part, and `MM_WORKER_SLOTS_*` and
`MM_WORKER_COOLDOWN` count only where they are set on the target. Only the rule
`cli` is not followed: a task sent with `-H` always runs under `claude`.

A sent task holds a slot on the target from before it starts until it ends, and none
on the dispatching machine. So tasks started on the target and tasks sent to it share
that machine's limit. Two dispatching machines sending to one target do too.

`mm-worker` doing this sends a copy of itself to the target and runs it with `--remote`,
so the target needs `python3`, 3.9 or newer, but no particular version of `mm-worker`.
A task with no free slot asks again every few seconds.

## What `fleet-agent.py` adds

Every snapshot gains:

```json
"dispatcher": {
  "installed": true,
  "version": 1,
  "claudeInstalled": true,
  "rules": { "...the effective rules..." },
  "rulesValid": true,
  "providers": {
    "minimax": {"configured": true, "used": 2, "cooling": false, "coolingReason": ""},
    "zhipu": {"configured": false, "used": 0, "cooling": false, "coolingReason": ""},
    "claude": {"configured": true, "used": 0, "cooling": false, "coolingReason": ""},
    "qwen": {"configured": false, "used": 0, "cooling": false, "coolingReason": ""}
  }
}
```

- `installed` / `version`: `~/.local/bin/mm-worker` exists and is a Python
  `mm-worker` declaring `MM_WORKER_VERSION = <n>`. Find this by reading the file,
  not by running it. A file without that line is the older shell version:
  `installed` true, `version` 0.
- `configured`: the env file exists and defines a non-empty key variable
  (`MINIMAX_API_KEY`, `ZHIPU_API_KEY`, `QWEN_API_KEY`). The value itself is never
  reported. `claude` counts as configured when `claudeInstalled` is true.
- `used`: live slots, the same count `mm-worker --status` shows.
- `cooling`: `<provider>_unavailable_until` is in the future. `coolingReason` is
  the text of `<provider>_unavailable_reason`.

New commands. Each prints one JSON line and exits 0, including on refusal:

- `rules-set <base64-of-json> <expected-revision>`: validate and write atomically
  (temp file in the same folder, then rename), with `revision` set to
  `expected-revision + 1`. Answers `{"ok": true, "revision": n}`, or
  `{"ok": false, "error": "..."}` if invalid, or if the file's revision isn't
  `expected-revision` (`"error": "conflict"`, `"revision": <current>`). A missing
  file has revision 0. The rules arrive as an argument because stdin carries the
  script. Base64 keeps them out of shell quoting.
  A rule with a full model name is refused while the installed dispatcher is
  older than version 2.
- `rules-reset <expected-revision>`: write the defaults the same way.
- `models`: ask each provider that has a key for its models, and answer
  `{"ok": true, "providers": {"zhipu": {"models": ["glm-5.3", "..."]}, "qwen":
  {"error": "HTTP 404"}}}`. Only this command contacts a provider, and only when
  the rules of a device are on screen or the user refreshes: `watch` does not. The
  key goes to the `https` host in that provider's env file and nowhere else, redirects
  are not followed, and it is never reported. Claude isn't asked, as a subscription
  has no key for this. The app offers a built-in list for it instead.

Installing is not an agent command, because stdin is taken. The app sends the
bundled file over ssh to a temp file in `~/.local/bin`, checks its byte count,
and only then renames it to `mm-worker`. A dropped connection can look like a
normal end of input, so without the check half a file could be installed. The
rename means a running dispatcher keeps its old file.

## `~/.cache/mm-worker/` layout

`fleet-agent.py` reads this, so `mm-worker.py` must keep it exactly:

- `slots/<provider>/<n>/pid`: one folder per live slot, created with an atomic
  `mkdir`, holding the dispatcher's pid. Dead pids are reaped. A folder with no
  pid yet gets 5 seconds' grace. For a job sent from another machine it holds the
  job's pid instead, and `slots/<provider>/<n>/job` names the job's folder.
- `select.lock`: a folder taken with `mkdir` around choosing a provider and taking
  a slot. Treat it as stale after 30 seconds.
- `runs/<run_id>.prompt.md`, `.name` (from `-n`), `.stderr`, `.<provider>.json`
  (the event stream while running, then only its last line),
  `.remote` (`<host> <rjob> <provider>`) and `.cancelled`.
- `run_id` is `YYYYMMDD-HHMMSS-<dispatcher pid>`.
- `jobs/<run_id>-<provider>/` on a machine running work for another:
  `prompt.md`, `name`, `pid`, `started`, `claim.json`, `out.json`, `stderr`, `rc`.
  `claim.json` is the answer to the dispatcher, kept so that a retry after a dropped
  connection doesn't start the task twice.
- `queued/<run_id>/` on the same machine: `mm-worker.py`, `prompt.md` and `name`,
  for as long as the dispatcher is working with that machine. Removed after 7 days if
  it never came back.
- `<provider>_unavailable_until` (epoch seconds), `<provider>_unavailable_reason`.
- `ledger.tsv`: one line per run, tab-separated: `YYYY-MM-DD HH:MM:SS`, backend
  (`provider` or `provider@host`), model, status, `<n>s`, `in=<n>`, `out=<n>`,
  working folder with the home folder as `~`, first 60 bytes of the prompt with
  newlines and tabs as spaces. `in` includes both cache counts.
