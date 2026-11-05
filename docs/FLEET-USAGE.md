# Fleet: token usage across machines and accounts

The contract between `fleet-agent.py` and the Usage page of the Fleet window.

## What the agent adds to a snapshot (AGENT_VERSION 5)

`host` gains `"user"`: the OS account name running the agent.

Every snapshot gains `claudeAccount`:

```json
"claudeAccount": {"email": "a@example.com", "plan": "Claude Max", "organization": "Acme"}
```

Read from `oauthAccount` in `~/.claude.json`: `emailAddress`, `planDisplayName` and
`organizationName`, and nothing else from that file. It is `null` when the file, the
object or the email is missing: that means "not known", not "not logged in", because
a machine can log in with a token. Each field other than `email` may be `null`.
Re-read at most every 60 seconds. Skip a file over 32 MiB and report `null`.

Some snapshots gain `usageHistory`:

```json
"usageHistory": {
  "complete": true,
  "progress": 1.0,
  "days": {
    "2026-10-31": {"claude-opus-5-5": {"input": 1, "output": 2, "cacheRead": 3, "cacheWrite": 4}}
  }
}
```

- `days` covers the 29 local days before today on that machine. Today is not in it:
  today stays in `todayByModel`, which keeps its current meaning. Days and models with
  no tokens are left out.
- The field is sent only when its content has changed, so most snapshots have none. The
  app keeps the last one it received. Each one replaces the previous one in full.
- While the first scan runs, `complete` is false, `progress` is 0.0 to 1.0 by bytes, and
  `days` holds what is counted so far. Send one of these at most every 15 seconds.
- A message's day is the local day of its earliest timestamp. Counting is per message id,
  keeping the latest usage, across all files, exactly as `SessionLog` counts today, and
  `<synthetic>` is skipped. A message counted in `todayByModel` is never also in `days`.
- At local midnight, what was today moves into `days` without a full rescan being needed
  by the app, and the oldest day drops out.

## opencode

opencode writes no Claude Code logs. Its tokens are in `opencode.db` under
`$XDG_DATA_HOME/opencode`, or `~/.local/share/opencode`: one `message` row per reply,
with `modelID` and `tokens` in its `data`. `todayByModel` and `usageHistory` add these
to the rest, by the model opencode names, on the local day of `time_created`. Tokens
`reasoning` count as `output`, as opencode's `tokens.total` is the sum of the fields.

The query adds them up afresh, so nothing is remembered and nothing is counted twice.
It runs only after the database or its write-ahead log changed, at most every 30
seconds, and opens the database read-only. A database that is missing, busy or not
understood reports no tokens, or the last ones read, and never stops a snapshot.
Sessions of opencode are not listed.

## Cost to the machine

This runs on machines doing other work, some with hundreds of MB of logs and little
memory, so it must not get in their way:

- Only files with an mtime within the 30-day window are opened.
- The scan reads at most 16 MiB per pass of the main loop, so live snapshots keep their
  normal rhythm while it runs. Long logs take several passes.
- Lines without `"usage"` are skipped unparsed, as `SessionLog` does.
- After the first scan, per-message detail for past days is dropped wherever that can be
  done without miscounting a later duplicate; memory must not grow without limit.
- The agent stays read-only. It writes nothing to the machine.

`--once` finishes the whole scan before printing and always includes `usageHistory`.

The scan starts again on every connection, as the agent keeps nothing on the machine.
Until it finishes, the app keeps showing the days it already has, so totals don't fall
and climb back each time the window opens.

## How the app groups tokens

A **device** is one entry in `fleet.json`: one OS account reached over ssh. Two devices
with the same `host.hostname` are one **machine** with several accounts. Two devices with
the same `host.hostname` and `host.user` are the same logs listed twice, and only the
first in `fleet.json` counts.

The page has three views:

- **Machines:** one row per machine, named after its first device's label, with its OS
  users as children when it has more than one. To see a machine's other users, add a device
  for each to `fleet.json`, by its own ssh alias or with `sudoUser`.
- **Accounts:** tokens by who pays for them. Models whose vendor is Anthropic belong to the
  device's `claudeAccount.email`, so one subscription used on three machines is one row with
  three machines as children. Devices with no known email are one row each, "Unknown Claude
  account", with the device's label as detail. Models from any other vendor belong to one
  row per vendor (for example MiniMax), as the app can't tell API keys apart.
- **Models:** one row per vendor, with its models as children.

Totals in all three views are equal for the same range and metric.
