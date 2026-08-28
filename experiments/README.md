# experiments/ — raw run data

Raw, unedited output from ZugaMind's published experiments. The write-ups
(design, pre-registered predictions, results) live in
[docs/experiments/](../docs/experiments/); this folder holds the data those
documents cite.

Layout per experiment: one folder per run set. Inside, `<COND>-run<N>.jsonl`
is the experiment runner's per-tick measurement log for condition `COND`,
repeat `N`; the matching `<COND>-run<N>/engine/` folder holds ZugaMind's own
internal journal and final state from inside that run. Folders suffixed
`-invalid` are runs that were invalidated and preserved with a post-mortem
(see their READMEs) — kept deliberately, per the pre-registration discipline.

These files are receipts: nothing in here is edited after a run, including
`summary.json` files whose `raw` paths record where the run happened at the
time it was executed (they predate this folder's creation and are left
verbatim). The loose `*.log` files are console output from launching the
runs — with one exception: `exp001-accept-B.log` is a crash, not a run.
An acceptance-pass launch died before any tick (a list-shaped harness
config where `command_actuator.invoke_harness` expects a dict); it has
no data folder and is kept only as the record of that attempt. The
acceptance data that exists is `exp001-accept-A/`.
