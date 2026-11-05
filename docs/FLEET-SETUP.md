# Fleet auto-setup: contract

Goal: TokNotch's Fleet window discovers which coding tools each machine has, proposes a
configuration for that machine, and applies it after the user confirms, one machine at a time.
First tools: claude, opencode, codex.

Nothing is written to a machine without that confirmation. Discovery is read-only and runs no
tool's command. Sign-ins and API keys are never automated, copied or shown: the app only says
what is missing.

## Agent: `tools` in every snapshot (fleet-agent.py)

```json
"tools": {
  "claude":   {"installed": true,  "hasData": true,  "lastUsed": 1793865706.1, "signedIn": true},
  "opencode": {"installed": true,  "hasData": true,  "lastUsed": 1793865706.1, "signedIn": null},
  "codex":    {"installed": false, "hasData": false, "lastUsed": null,         "signedIn": false}
}
```

- `installed`: the command exists on PATH or in that tool's usual folders. It is never run, so
  there is no version. Reuse `claude_installed()` and `opencode_installed()`; add codex the same way.
- `hasData`, `lastUsed` (epoch seconds, or null): newest usage data. claude: `*.jsonl` under
  `~/.claude/projects`. opencode: `OPENCODE_DB` and its `-wal`. codex: `*.jsonl` under
  `$CODEX_HOME/sessions` or `~/.codex/sessions`.
- `signedIn`: true, false, or null for "can't tell". Existence and non-emptiness of a file only,
  and no credential file is ever parsed or reported.
  - claude: true if `~/.claude/oauth-token` is non-empty, `~/.claude/.credentials.json` exists,
    or the existing `ClaudeAccount` found an account. Otherwise null on macOS (it may be in the
    Keychain) and false elsewhere. A login that expired still reads true: document this.
  - opencode: true if `auth.json` exists beside `OPENCODE_DB`, or a provider key is configured
    (the check `Dispatcher` already does). Otherwise null.
  - codex: true if `auth.json` exists in `$CODEX_HOME` or `~/.codex`. Otherwise false.
- Re-read at most every 60 seconds: these machines are doing other work. The walk of a large
  log folder must not hold up a snapshot; stop at a bound and keep the answer so far.
- `dispatcher` gains `"remoteRunner": true|false`: `~/.local/bin/mm-remote-run` is executable.

`AGENT_VERSION` becomes 6. Older apps ignore unknown fields.

## Agent: codex usage (fleet-agent.py)

Codex sessions are JSON lines. What matters, with real field names:

```json
{"timestamp":"2026-10-29T19:12:56.392Z","type":"turn_context","payload":{"model":"gpt-6.1-sol","cwd":"..."}}
{"timestamp":"2026-10-29T19:13:02.293Z","type":"event_msg","payload":{"type":"token_count","info":{
  "total_token_usage":{"input_tokens":33278,"cached_input_tokens":22016,"cache_write_input_tokens":0,"output_tokens":191,"reasoning_output_tokens":68,"total_tokens":33469},
  "last_token_usage":{"input_tokens":17114,"cached_input_tokens":16000,"cache_write_input_tokens":0,"output_tokens":60,"reasoning_output_tokens":0,"total_tokens":17174},
  "model_context_window":258400}}}
```

- Each `token_count` with `info` is one reply. Count its `last_token_usage`. `info` may be null: skip.
- The same event can repeat. Skip one whose `total_token_usage` equals the previous in that file.
- `input` = `input_tokens` - `cached_input_tokens`. `cacheRead` = `cached_input_tokens`.
  `cacheWrite` = `cache_write_input_tokens`. `output` = `output_tokens`, which already includes
  reasoning (`total_tokens` = `input_tokens` + `output_tokens`), so do not add reasoning.
- The model is that of the latest `turn_context` before the event in the same file; "unknown" if none.
- The day is the local day of the event's `timestamp`.
- Lines are large and contain conversation text. Skip unparsed any line containing neither
  `"token_count"` nor `"turn_context"`, and report nothing but counts and model names.
- Read incrementally, by offset, as `UsageHistory` does, within `HISTORY_PASS_BYTES` per pass, and
  only open files with an mtime inside the window. Totals feed `todayByModel` and `usageHistory`
  the way `OpencodeUsage` does (see `read_opencode` and `snapshot`).
- `complete` and `progress` of `usageHistory` must include this scan.

## mm-remote-run in the repo

`Sources/Resources/mm-remote-run`: bash, the target-side companion that `docs/FLEET.md` describes
under "Running a task on another machine". Same contract, and `provider_env()` in `mm-worker.py` is
the reference. Providers: minimax, zhipu, qwen, claude. Comments in English, nothing personal, no
hard-coded host. Keys come only from `~/.config/<provider>/env`. For claude, use
`~/.claude/oauth-token` as `CLAUDE_CODE_OAUTH_TOKEN` if it is non-empty. Use `timeout` when the
machine has it; macOS has none, so fall back to a `perl` alarm. The app installs it only where
none exists and **never overwrites one**, since existing ones are hand-tuned.

## Swift: decoded data (owned by the logic task)

```swift
struct FleetTool: Codable, Equatable {
    var installed: Bool
    var hasData: Bool
    var lastUsed: Double?
    var signedIn: Bool?
}
// FleetSnapshot gains:  var tools: [String: FleetTool]?   (unreadable means absent, as for `dispatcher`)
// FleetDispatcher gains: var remoteRunner: Bool?          (absent on an older agent)
```

## Swift: the proposal (Sources/Fleet/FleetSetup.swift, pure logic, owned by the logic task)

```swift
enum FleetSetupChange: Equatable, Hashable {
    case disableProvider(String)
    case setCLI(String)
    case addToLevel(level: String, provider: String, model: String)
}
enum FleetSetupNeed: Equatable, Hashable {
    case signIn(tool: String)   // installed, and signedIn == false
    case addKey                 // no provider left that can work
}
struct FleetSetupProposal: Equatable {
    var installDispatcher: Bool
    var installRunner: Bool
    var changes: [FleetSetupChange]
    var rules: FleetRules?      // the whole rules to write, nil if `changes` is empty
    var needs: [FleetSetupNeed]
    var isEmpty: Bool { !installDispatcher && !installRunner && changes.isEmpty && needs.isEmpty }
}
enum FleetSetup {
    /// Nil if the snapshot lacks `tools` (an older agent), so nothing can be said.
    static func proposal(for snapshot: FleetSnapshot,
                         bundledDispatcher: Int = FleetRules.bundledDispatcherVersion) -> FleetSetupProposal?
}
```

Rules, applied in this order to `snapshot.dispatcher`'s effective rules:

1. `installDispatcher`: the dispatcher isn't installed, or its version is below the bundled one.
2. `installRunner`: `remoteRunner == false`. Not when absent.
3. `setCLI("opencode")`: tools.claude isn't installed, tools.opencode is, `rules.cli` isn't already
   opencode. Nothing ever proposes moving away from opencode: a machine may have claude installed
   and unusable, as a region-blocked one does.
4. `disableProvider(p)`: p is enabled and can't work here: its `configured` is false, or p is claude
   and the cli is opencode. Never disable the last enabled provider if that leaves none: propose
   nothing for it and add `.addKey` instead.
5. `addToLevel`: after 3 and 4, a level has no enabled provider. Add the first provider in `order`
   that is enabled, with the model `m3` for minimax, `flash` for zhipu, `flash` for qwen, `sonnet`
   for claude (`claude-opus-5-5` for hard).
6. Needs: `.signIn(tool)` for each tool in claude, opencode, codex order with `installed` and
   `signedIn == false`. `.addKey` once, if no enabled provider can work.

Never touched: slots, balance, order, defaults, models already in a level, and a provider the user
disabled. Nothing is ever enabled. `rules` keeps the same `revision`; it must pass
`FleetRules.validate()`, and if it doesn't, propose no rule changes at all.

Applying a proposal and asking again must give no changes (idempotent). Test this. Skip rules 3-5
when `rulesValid` is false, as the effective rules are then only defaults.

## Swift: UI and applying (owned by the UI task)

- In a machine's detail view, a "Setup" section above its rules: detected tools, one row each
  (name, installed, last used, signed in or not), then what the proposal suggests in plain words.
- One button, "Apply…", opening a confirmation listing every change for that machine. There is no
  "apply to all" and nothing runs by itself. A machine whose rules are read-only (`sudoUser`) or that
  isn't live shows the proposal without the button. `needs` are shown as text, with no button.
- Apply order: install dispatcher, install runner, write rules (`rules-set` with the snapshot's
  revision). Stop at the first failure and show it. A conflict shows the existing conflict notice.
- The runner install uses the same safe path as the dispatcher (temp file in `~/.local/bin`, byte
  count check, rename), plus: refuse if `~/.local/bin/mm-remote-run` already exists, and report
  that as "already there", not a failure.
- New strings in `en`, `zh-Hans` and `zh-Hant`. Other languages fall back to English.
