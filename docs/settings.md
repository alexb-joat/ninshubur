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

## What `apply` will do

`apply KEY=VALUE… --yes` makes the same checks and then:

- writes the changes to `settings.yaml` in the data home, a second file merged over `config.yaml` when a process starts.
  `config.yaml` is never written. The file is written whole to a temporary file and renamed into place, mode 0600 on
  Linux and macOS;
- appends one line to `logs/settings.jsonl`: an id, the time, each key with its old and new value, and your `--note`.
  The line holds no environment values and no secrets;
- changes nothing in any process that is already running. **A new value takes effect at the next process start** (the
  next `ninshubur` command, or when the daemon is restarted); nothing is reloaded live.

Without `--yes` it prints the preview and exits with status 2, saying so.

`undo` restores the values the last apply (or the apply `ID`) replaced, logs that too, and removes `settings.yaml` when
nothing is left in it. `log` prints the audit lines.

A hand-edited `settings.yaml` is read with the same checks: an entry for a guardrail key, an unknown key or a value out of
bounds is ignored with a warning and never applied. `apply` refuses to rewrite a `settings.yaml` that holds such entries
until you have removed them, so it never drops something you wrote by accident.
