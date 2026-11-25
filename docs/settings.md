# Settings you can change by command

`ninshubur settings` is for the few numbers you tune as you learn what is worth your time: what a kind of suggestion is
worth, what your hour is worth, what a trip or a task costs. It is a command line only; there is no other interface.

```
ninshubur settings list                 # each setting, its effective value and where the value came from
ninshubur settings list --all           # also the guardrail keys, read-only
ninshubur settings preview KEY=VALUE…   # old -> new, checked; writes nothing
ninshubur settings apply KEY=VALUE… --yes [--note TEXT]
ninshubur settings undo [ID]
ninshubur settings log [--last N]
```

## Two classes of key

A **learnable** key changes how suggestions are valued and costed and nothing else. The list below is the whole list: a
key is learnable only if it is in it.

<!-- gen:settings-keys -->
| key | type | bounds | default | meaning |
|---|---|---|---:|---|
| `base_value_usd.cfp_submit` | number | 0 to 100000 | 800 | the starting value in USD of a suggestion of kind `cfp_submit`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.conference_prospecting` | number | 0 to 100000 | 600 | the starting value in USD of a suggestion of kind `conference_prospecting`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.hiring_signal_outreach` | number | 0 to 100000 | 250 | the starting value in USD of a suggestion of kind `hiring_signal_outreach`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.local_meeting_suggestion` | number | 0 to 100000 | 150 | the starting value in USD of a suggestion of kind `local_meeting_suggestion`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.outreach_window` | number | 0 to 100000 | 200 | the starting value in USD of a suggestion of kind `outreach_window`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.pain_content_idea` | number | 0 to 100000 | 60 | the starting value in USD of a suggestion of kind `pain_content_idea`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.service_request_lead` | number | 0 to 100000 | 400 | the starting value in USD of a suggestion of kind `service_request_lead`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.tool_deprecation` | number | 0 to 100000 | 300 | the starting value in USD of a suggestion of kind `tool_deprecation`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.tool_outage` | number | 0 to 100000 | 50 | the starting value in USD of a suggestion of kind `tool_outage`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.tool_price_change` | number | 0 to 100000 | 100 | the starting value in USD of a suggestion of kind `tool_price_change`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `base_value_usd.tooling_adopt` | number | 0 to 100000 | 100 | the starting value in USD of a suggestion of kind `tooling_adopt`, before the weight, relevance, confidence, timing, tier and calibration factors |
| `budgets.daily_human_minutes` | whole number | 0 to 1440 | 45 | your daily budget of human minutes, shown by `ninshubur budget` against the minutes logged today |
| `budgets.hourly_rate_usd` | number | 0 to 10000 | 60 | what an hour of your time is worth in USD; it turns a suggestion's minutes into its human cost |
| `corroboration_value_usd` | number | 0 to 100000 | 300 | the value in USD above which a suggestion resting on a single Tier 2 or 3 source is held as needing corroboration (the policy's own limit still applies) |
| `costs.minutes.cfp_submit` | whole number | 0 to 1440 | 120 | the minutes of your time a suggestion of kind `cfp_submit` is costed at |
| `costs.minutes.conference_prospecting` | whole number | 0 to 1440 | 20 | the minutes of your time a suggestion of kind `conference_prospecting` is costed at |
| `costs.minutes.hiring_signal_outreach` | whole number | 0 to 1440 | 15 | the minutes of your time a suggestion of kind `hiring_signal_outreach` is costed at |
| `costs.minutes.local_meeting_suggestion` | whole number | 0 to 1440 | 20 | the minutes of your time a suggestion of kind `local_meeting_suggestion` is costed at |
| `costs.minutes.outreach_window` | whole number | 0 to 1440 | 15 | the minutes of your time a suggestion of kind `outreach_window` is costed at |
| `costs.minutes.service_request_lead` | whole number | 0 to 1440 | 10 | the minutes of your time a suggestion of kind `service_request_lead` is costed at |
| `costs.minutes.tool_outage` | whole number | 0 to 1440 | 0 | the minutes of your time a suggestion of kind `tool_outage` is costed at |
| `costs.travel_abroad_usd` | number | 0 to 100000 | 600 | the cost in USD of an event abroad, or where the place is unknown |
| `costs.travel_same_city_usd` | number | 0 to 100000 | 0 | the cost in USD of an event in your own city |
| `costs.travel_same_country_usd` | number | 0 to 100000 | 150 | the cost in USD of an event elsewhere in your country |
<!-- /gen -->

Every other key of the configuration is a **guardrail** key. It changes what is fetched, stored, sent or admitted, so
`settings` shows it (with `list --all`) and never sets it: the `llm` block (endpoints, models, caps, the person-data
rule), `sinks`, `sources` (with each source's trust block), `scheduler`, `retention_months`, `budgets.daily_bytes`,
`user_agent`, `timezone`, `api`, and any key you added that is not in the table above. The policy (`policy.yaml` and the
shipped floor) is a separate file that `settings` never reads or writes. To change a guardrail key, edit `config.yaml` by
hand; its comments are yours.

Two of the learnable keys do less than their group suggests, as of this build:

- `budgets.daily_human_minutes` is shown by `ninshubur budget` against the minutes you logged today. It does not change
  a suggestion's value or its rank.
- `corroboration_value_usd` changes whether a suggestion resting on one Tier 2 or 3 source is held as needing
  corroboration, not what it is worth; the policy's own `corroboration.value_usd` is a ceiling it cannot raise.

## Preview

`preview` checks every `KEY=VALUE` and prints what would change, with the value now and where it came from (`default`,
`config` for `config.yaml`, or `overlay` for `settings.yaml`). It writes nothing. It refuses, with the reason and exit
status 2, a guardrail key, a key that is not in the configuration (naming the nearest one when it looks like a typo), a
group such as `base_value_usd` (naming its settings), a value of the wrong type, and a value outside the bounds. If any
assignment is refused, nothing is applied.

## What `apply` does

`apply KEY=VALUE… --yes` makes the same checks as `preview`, and then:

- writes the changes to `settings.yaml` in the data home, a second file that is merged over `config.yaml` whenever a
  process starts. `config.yaml` is never written. The file is written whole to a temporary file and renamed into place,
  mode 0600 on Linux and macOS. It is rewritten each time, so keep your own comments in `config.yaml`;
- appends one line to `logs/settings.jsonl`: an id, the time, each key with its old and new value (and where the old one
  came from), and your `--note` (one line, up to 200 characters). The line holds no environment values and no secrets.
  The line goes down after the new file is written and before the rename, so a change never happens without its line; if
  the line cannot be written, nothing changes (exit 1);
- skips a key whose value is already the one asked for, and writes and logs nothing if that is every key;
- changes nothing in any process that is already running. **A new value takes effect at the next process start** (the
  next `ninshubur` command, or when the daemon is restarted); nothing is reloaded live, and the daemon's reload of
  `config.yaml` does not carry a new value into the running process either.

Without `--yes` it prints the preview and exits with status 2, saying so.

## Undo and log

`undo` restores what the last apply replaced (a value from `config.yaml` or the default comes back when the apply had put
the key in `settings.yaml` for the first time), logs that as its own line, and removes `settings.yaml` when nothing is
left in it. `undo ID` does the same for the apply with that id (see `log`). It is refused, with the reason, when:

- a key of that apply has changed again since (a later apply, or a hand edit): undo the later one first;
- that apply was already undone, or no such apply is logged;
- the audit line is not usable (a hand-edited line naming a guardrail key, or a value out of bounds).

`undo` with no id means the last apply; if that one is already undone it says so and you name an earlier one.

`log [--last N]` prints the last N audit lines (default 10), oldest first, with what each did and which were undone. A
line that cannot be parsed is skipped.

`apply --yes` and `undo` take turns: each one holds the file `settings.lock` in the data home while it reads
`settings.yaml` and the log, numbers its line and writes, and removes it when it ends. So two of them started at the same
moment never get the same id, and neither overwrites the other's change. A second one that finds the file refuses with
exit status 2 and writes nothing; run it again when the first has finished. If no other `settings apply` or `undo` is
running (a command was killed and left the file behind), the refusal names the file: delete it and run the command again.
`list`, `preview`, `log` and `apply` without `--yes` only read, and don't need the file.

## A hand-edited `settings.yaml`

The file is read with the same checks as `apply`: an entry for a guardrail key, an unknown key or a value out of bounds is
ignored with a warning (on standard error, and in `settings list`) and never applied; the entries that pass are used.
`apply` and `undo` refuse to rewrite a `settings.yaml` that holds such an entry, or that is not valid YAML, until you
have removed it, so they never drop something you wrote by accident.
