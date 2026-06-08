# museumvocab-reconcile

Generalises the technique-vocabulary → Getty AAT pipeline into a reusable tool
for reconciling MuseumPlus museum vocabularies (materials, object names,
subjects, …) against authority sources (AAT, Iconclass, later ULAN/TGN/Wikidata).

Three design goals drive the structure:

1. **Generalisable** — a fixed engine + per-vocabulary YAML *profiles* +
   pluggable *authority adapters*. Onboarding a new vocabulary means writing a
   profile, not editing code.
2. **Transparent** — every stage writes a versioned, human-readable artifact;
   the log records the exact config (thresholds, facet rules, endpoints) used.
3. **Human-in-the-loop at each step** — the pipeline is re-entrant; you can stop
   after any stage, inspect or hand-edit its artifact, and resume. Human
   overrides live in an external CSV, separate from machine output.

## Pipeline (re-entrant stages)

```
prep           source.json                        -> 01_prepared.json
(translate)    01_prepared.json                   -> 01b_translations.csv   (optional, step 1b)
(translate-apply) + 01b_translations.csv          -> 01b_translated.json
lookup         01_prepared.json (or 01b_…)        -> 02_candidates.json   (network; run on your machine)
classify       02_candidates.json                 -> 03_classified.json
review-export  03_classified.json                 -> 03b_review.csv        (edit by hand)
assemble       03_classified.json + 03b_review.csv -> 04_final.json (+ 04_final.csv, 04_linkedart.json, log.txt)
```

Each stage reads the previous artifact and writes the next, so you can stop
after any stage, inspect or hand-edit its artifact, and resume. The optional
translation step (1b) is described under *LLM-recommended English* below.

### Why lookup runs on your machine
The authority endpoints (Getty, Iconclass) aren't reachable from every
environment — some networks and CI sandboxes block them — so `lookup` (and the
`translate` step, which calls an LLM API) are the parts you run where outbound
network is available. Everything else (`prep`, `classify`, `review-export`,
`assemble`, `flag-anomalies`) is fully offline.

## Install & run

Install once from the repo root (the folder containing `pyproject.toml`):

```bash
pip install -e .
```

Profiles ship inside the package, so you can pass a profile by **bare name**
from any folder — `--profile techniques.aat.yaml` resolves to the bundled
profile. Pass a real path only when using your own custom profile.

### bash / macOS / Linux

```bash
P=techniques.aat.yaml
museumvocab-reconcile --profile $P prep   ConObjectTechniqueVgr.json
museumvocab-reconcile --profile $P lookup
museumvocab-reconcile --profile $P classify
museumvocab-reconcile --profile $P review-export        # edit 03b_review.csv
museumvocab-reconcile --profile $P assemble
```

### PowerShell (Windows)

PowerShell variables use `$name = "value"` (with the `$` and spaces). Bash-style
`P=...` does **not** set a variable — if `$P` is empty, `--profile` swallows the
next word and you get `invalid choice: ...`. Verify a variable by typing `$P`.

```powershell
$P = "techniques.aat.yaml"
museumvocab-reconcile --profile $P prep ConObjectTechniqueVgr.json
museumvocab-reconcile --profile $P lookup
museumvocab-reconcile --profile $P classify
museumvocab-reconcile --profile $P review-export        # edit 03b_review.csv
museumvocab-reconcile --profile $P assemble
```

Or inline, without a variable:

```powershell
museumvocab-reconcile --profile techniques.aat.yaml prep ConObjectTechniqueVgr.json
```

Notes for Windows:
* The source file (`ConObjectTechniqueVgr.json`) and the stage outputs
  (`01_prepared.json`, `02_candidates.json`, …, `cache.json`) are read/written
  relative to your **current folder**, so `cd` to a clean working folder first.
* Only `lookup` needs network (it calls Getty/Iconclass). `prep`, `classify`,
  and `assemble` are offline.
* If you see `invalid choice`, `$P` was empty. If you see `Profile not found`,
  the error lists the bundled profile names you can pass.

## Optional step 1b: LLM-recommended English (translate)

Many source terms lack an English main term. The optional `translate` step asks
an LLM for a recommended English label for those terms, using the Norwegian
term plus its parent chain and same-parent siblings as context. It needs the
`anthropic` package and an API key:

```bash
pip install -e ".[llm]"
# PowerShell: $env:ANTHROPIC_API_KEY = "sk-ant-..."
# bash:       export ANTHROPIC_API_KEY=sk-ant-...
```

It runs between `prep` and `lookup`, with a review gate:

```
translate        01_prepared.json                      -> 01b_translations.csv   (review/edit this)
translate-apply  01_prepared.json + 01b_translations.csv -> 01b_translated.json
lookup --inp 01b_translated.json
```

In `01b_translations.csv`, `accept` is pre-filled `yes` and `approved_english`
is pre-filled with the suggestion; edit `approved_english` to override, or set
`accept` to no/blank to skip a term. `translate-apply` folds approved English
into the prepared terms, tagged `target_source = llm` (or `human` if you edited
it). The translation is used by `lookup` only as an **untrusted** query — it can
surface AAT candidates but never auto-accepts, since only nb/nn are trusted. The
provenance shows up as `english_source` in `03b_review.csv` and
`translation_source` in the final outputs.

Settings live in the profile `translation:` block (model, `context`,
`batch_size`, sibling options, `domain_by_root`, `prompt_version`). Use
`translate --dry-run` to see how many terms (and roughly how many API calls)
before spending anything, and `--max-terms` for a small test. The API key is
read only from the `ANTHROPIC_API_KEY` environment variable — never put it in a
profile.

The model often flags terms that look misplaced in the source hierarchy. Pull
those into their own CSV for data-quality cleanup:

```
flag-anomalies   01b_translations.csv -> 01b_anomalies.csv
```

To refresh only some translations (e.g. after a prompt change) without
re-spending on the good ones, re-translate a subset by confidence and/or id and
merge the results back, leaving other rows (and their edits) untouched:

```
# re-translate the low/medium-confidence rows and overwrite the CSV in place
retranslate --confidence low,medium
# or specific ids:
retranslate --ids 100490880,100491296
```

`retranslate` always re-queries the selected rows (ignoring cache) with the
current prompt, and resets their `approved_english`/`accept` to the new
suggestion; rows it doesn't touch are preserved exactly.

## Tuning lookup speed & cache size

`lookup` is the slow stage: for every term it fetches each candidate concept and
walks its hierarchy, so both runtime and cache size scale with how many
candidates get enriched. Levers (in the profile `lookup:` block, each
overridable per run):

* `lookup.enrich_top_n` (`--enrich-top-n`) — enrich at most N candidates per
  term, highest score first. The single biggest lever.
* `lookup.min_candidate_score` (`--min-score`) — drop weak candidates before
  enriching.
* `lookup.result_limit` (`--limit`) — candidates requested per query.

The cache stores only compact per-concept nodes (labels, immediate broader,
scope note), not the full raw JSON-LD, so it stays small. Delete `cache.json`
once to discard any oversized cache from an earlier version.

**If many terms suddenly return 0 candidates**, the authority is likely
rate-limiting you. Do not delete `cache.json` (that forces a full re-fetch and
makes throttling worse) — keep it. Re-running `lookup` now surfaces persistent
failures as `ERROR 429/503/499` rather than silent zeros, and resume retries any
errored term. Slow down with `--sleep 0.5 --request-delay 0.3`, reduce request
volume with a higher `min_candidate_score` / lower `enrich_top_n`, and wait a
few minutes if you're currently throttled. To re-attempt terms that were
silently zeroed by an older build, delete `02_candidates.json` once (keep
`cache.json`) and re-run.

`assemble` writes `04_final.json`, `04_final.csv` (flattened, Excel-friendly),
`04_linkedart.json`, and `log.txt`.

## Config knobs that matter most

* `facets.accept_all` (default `false`) — accept any facet the authority returns
  without listing them; when `false`, only `facets.accepted` count.
* `facets.preferred` — influences tiering/ranking but never hard-filters.
* `thresholds.auto_accept.mode` — `full` (exact OR score/gap) · `exact_only`
  (only trusted-language exact matches) · `off` (everything → review).
* `languages.trusted_exact_match_langs` — languages in which an exact label
  match is trusted enough to auto-accept (`[]` for Iconclass — see below).

## Adapters

* **AAT (`adapters/aat.py`)** — implemented. Reconciles via the Getty OpenRefine
  endpoint (tolerant of JSON-body vs form-encoded request styles, remembering
  whichever works) and fetches each concept's JSON-LD. It parses both Getty
  serialisations: the GVP/SKOS model (preferred — labels carry `@nb`/`@nn`
  tags) and the Linked.Art model (languages mapped from AAT URIs). Facet is
  derived by walking `broader` to a known root in `FACET_ROOTS`.
* **Iconclass (`adapters/iconclass.py`)** — implemented. Per-notation JSON gives
  labels and the ancestor path; the text-search route is discovered from the
  live OpenAPI spec at run time. Iconclass has no Norwegian, so an Iconclass
  profile pivots on English (`trusted_exact_match_langs: []`).

Two caveats worth knowing:
* AAT facet detection only resolves a facet when an ancestor is in `FACET_ROOTS`
  (a small, editable map near the top of `adapters/aat.py`). Concepts whose root
  isn't listed get `facet: null` and, unless `facets.accept_all` is true, are
  routed to review. Extend the map for vocabularies in other facets.
* The Iconclass search route is resolved live; if results look wrong, check
  `https://iconclass.org/docs` and pass `search_template=` to the adapter.

## Status

All stages are implemented and in working use: prep (schema-drift tolerant
loader), optional LLM translation (`translate` / `translate-apply` /
`retranslate` / `flag-anomalies`), lookup (with retry/backoff, resume, and
compact caching), tiering, review export/ingest, and assembly (JSON + CSV +
Linked Art + log). There is no automated test suite yet; the modules are
structured so the engine logic (tiering, parsing, CSV round-trips) can be tested
without network access.
