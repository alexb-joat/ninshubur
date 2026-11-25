# The run report

`ninshubur export run-report` writes one dated page of counts about what this installation has done. It is built for
sharing: the page holds aggregates and nothing that names anything.

```
ninshubur export run-report                  # feeds/run-report-<YYYY-MM-DD>.md, everything stored
ninshubur export run-report --days 30        # the time-stamped sections limited to the last 30 days
ninshubur export run-report --format json    # the same numbers as JSON
ninshubur export run-report -o report.md     # another path
```

The command opens `ninshubur.db` read-only (`mode=ro`, `query_only`); it creates, migrates and changes nothing, so it
is safe to run while the crawler is running. The file it writes is readable by you only (mode 0600 on Linux and macOS).
The footer of the page says which tables were read and the command that rebuilds it.

## What each number means

- **Window.** The UTC time of the first and the last row in `fetches` inside the window. *Days with at least one
  fetch* counts UTC calendar days that have a `fetches` row. *Days fetched without human feedback* counts those days
  on which no row of `feedback` (your `accept`, `dismiss` and `done` decisions) was recorded. It says how many days of
  fetching have no recorded human decision beside them; it does not say whether anyone was looking.
- **Fetches.** One row per request the fetcher logged. *Status class* is the HTTP status divided into 1xx to 5xx;
  *no response* is a fetch that has no status (refused by the gate before any request, or the network failed).
  *By adapter* joins each fetch to the adapter of its source, so the page names the adapter (`greenhouse`, `lever`,
  and so on) and never the source; *non-2xx* is any response that is not a 2xx. A fetch whose source is no longer in `sources` is counted under "source no longer in
  the database". *With an error recorded* counts rows whose `error` column is not empty; the text is never printed.
  *Changed* counts fetches that changed what was stored; *bytes* is the sum of bytes received.
- **Refusals and quarantines.** *Fetches the gate refused* are rows whose error is `refused:<code>`; the code is one
  of the gate's closed list (`robots_disallow`, `host_not_in_spec`, the `url_*` codes, and so on). *Findings recorded on
  fetched documents* are rows whose error is a list of detector codes. *Quarantine records* are rows of `quarantine`,
  counted by their severity (`medium`, `high`, `integrity`) and by each detector code they carry (one record can carry
  several, so the code counts can add up to more than the record count). A code or severity outside the closed lists
  is counted as `other`, never printed.
- **Items.** Items first seen in the window, by kind, with how many of them are held or tainted.
- **Companies.** The number of canonical company groups in `company_aliases` (distinct `company_key`), and the number
  of aliases that map to them. It is as of the last roll-up and is not limited to the window.
- **Suggestions.** Suggestions created in the window, by kind and by status. `conference_trip` is a kind that earlier
  builds wrote and that databases from then still hold; `other` is any kind or status not in the lists the report knows.
- **Sources.** Sources as of now, by adapter and health. *Enabled* counts sources that are switched on. The adapter
  `explorer` is the explorer's inbox source (a row that is never fetched).
- **Model runs.** Rows of `llm_runs` started in the window, by model name: runs, answered, failed, input and output
  tokens, and the USD each run recorded for itself, summed. The cost is *as recorded*; it is not an invoice. A model
  name that is not a plain identifier (letters, digits and `. _ : / + @ -`, up to 64 characters) is counted under "other
  (not a plain model name)".
- **Explorer ladder.** The explorer's candidates by state (`candidate`, `probed`, `trial`, `admitted`, `rejected`,
  `quarantined`), read read-only from `explorer/explorer.db` when that file exists. Without it the page says
  "Not included" and why.

A section whose table is missing from the database (an older schema) is left out with one line saying why.

## What the page does not show

- Any item title, link, host, or name of a company, person, source or profile. The report never selects those columns.
- Any free text: `fetches.error`, `sources.health_reason`, notes, reasons and excerpts are not read into the report.
- Why a fetch failed beyond its status class and, for refusals and findings, its code.
- Whether the numbers are good. The page counts; it does not judge. It says nothing about the quality of a suggestion or
  a qualifier answer (`docs/case-study.md` is where that is measured).
- Anything the crawler did not record: a fetch that never reached the logging step is not in `fetches`.
- The time-stamped sections follow `--days`; sources, companies and the explorer's ladder are counts as of the moment of
  the report.
- Where the data came from. A page built from a copy of a database says what that copy held, not what the live file
  holds now.

## Rebuilding

The page records the time it was generated and the version that generated it. The default file name carries the UTC
date, so building it again on another day gives a new file and leaves the earlier one alone; building it again on the same
day, or with the same `-o` path, replaces that file.
