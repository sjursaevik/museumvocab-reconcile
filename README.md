# museumvocab-reconcile

This command-line tool links a museum's controlled-vocabulary terms to concepts in an external
authority — the [Getty Art & Architecture Thesaurus (AAT)](https://www.getty.edu/research/tools/vocabularies/aat/)
or [Iconclass](https://iconclass.org/) — with a human reviewing every uncertain
match, and emits the result as [Linked Art](https://linked.art/) JSON-LD.

It was built at the Norwegian National Museum of Art Architecture ad design to reconcile MuseumPlus vocabularies
(techniques, materials, object names, subjects) but is not specific to one
vocabulary: a **profile** (a YAML file) describes each source-vocabulary →
authority mapping, so onboarding a new vocabulary means writing a profile, not
changing code.

A few terms used throughout:
* **authority** — the external reference vocabulary you link *to* (AAT, Iconclass).
* **reconcile** — search the authority for the concept that matches a source term.
* **facet** — the authority's own top-level category for a concept (e.g. AAT's
  *Materials*, *Activities*). A profile lists which facets it will accept.
* **tier** — the confidence verdict the tool assigns each term: `auto_accept`,
  `review`, or `no_match`.

## How matching is trusted (the core idea)

Nasjonalmuseet contributed the Norwegian Bokmål (`nb`) and Nynorsk (`nn`) labels for techniques and materials
that now live in Getty AAT. Because those labels are human-catalogued and known
to be ours, an **exact match on a Norwegian label is the strongest possible
signal** — strong enough to accept automatically. Everything else is treated
with more caution:

* An exact `nb`/`nn` label match (in an accepted facet) → **auto-accept**.
* An exact match on the term's existing, human-catalogued English → auto-accept.
* A strong score with a clear gap to the runner-up → auto-accept. (can be turned off)
* Anything weaker, ambiguous, out-of-facet, or surfaced only via machine-
  translated English → **sent to a human for review**.

Machine-translated English (the optional translate step) is only ever a *search
query* to surface candidates a Norwegian query missed — a match found that way
**never** auto-accepts, even on an exact hit.

> **Worked example.** The term *Bladgull* ("gold leaf") is queried in Norwegian,
> matches the `nb` label on AAT concept *gold leaf* exactly, and that concept
> sits in the accepted *Materials* facet → auto-accepted, emitted as
> `made_of: gold leaf`. No human needed.

For an authority with no Norwegian (Iconclass), a profile simply pivots on a
different trusted language — this is per-profile config, not hardcoded.

## The pipeline

The work is split into stages. Each stage reads the previous stage's file and
writes the next, so you can **stop after any stage, inspect or hand-edit its
output, and resume** — the human-in-the-loop happens by editing these files.

```
prep            source export        -> 01_prepared.json
 ├ (optional translate steps, below) -> 01b_translated.json
lookup          prepared terms       -> 02_candidates.json     (needs network)
classify        candidates           -> 03_classified.json
review-export   classified terms     -> 03b_review.csv         (edit by hand)
assemble        classified + review  -> 04_final.json, 04_final.csv,
                                        04_linkedart.json, log.txt
```

Only `lookup` (and the optional `translate`) reach the network: `lookup` calls
the authority, `translate` calls an LLM. Every other stage runs fully offline,
so they work anywhere even when a CI sandbox or office network can't reach
Getty. Run `lookup`/`translate` wherever outbound network is available.

## Install

From the repo root (the folder with `pyproject.toml`):

```bash
pip install -e .
```

Profiles ship inside the package, so you can pass one by **bare name** from any
folder (`--profile techniques.aat.yaml`); pass a real path only for your own
custom profile. The bundled profiles are `techniques.aat.yaml`,
`materials.aat.yaml`, `objectnames.aat.yaml`, and `subjects.iconclass.yaml`.

## Run a vocabulary through, start to finish

The stage files are read and written **relative to the current folder**, so
work in a clean directory. `--profile` must come *before* the stage name.

### PowerShell (Windows)

PowerShell variables are `$name = "value"`. Bash-style `P=...` does **not** set
one — if `$P` is empty, `--profile` swallows the next word and you get
`invalid choice`. Check a variable by typing `$P`.

```powershell
$P = "techniques.aat.yaml"
museumvocab-reconcile --profile $P prep ConObjectTechniqueVgr.json
museumvocab-reconcile --profile $P lookup
museumvocab-reconcile --profile $P classify
museumvocab-reconcile --profile $P review-export     # edit 03b_review.csv, then:
museumvocab-reconcile --profile $P assemble
```

### bash / macOS / Linux

```bash
P=techniques.aat.yaml
museumvocab-reconcile --profile $P prep ConObjectTechniqueVgr.json
museumvocab-reconcile --profile $P lookup
museumvocab-reconcile --profile $P classify
museumvocab-reconcile --profile $P review-export     # edit 03b_review.csv, then:
museumvocab-reconcile --profile $P assemble
```

`museumvocab-reconcile --help` lists every stage; `… <stage> --help` explains
one stage and its options. Common errors: `invalid choice` means `$P` was empty;
`Profile not found` lists the bundled names you can pass.

`prep` cleans known MuseumPlus export quirks as explicit steps: it strips literal
`"NULL"` cells, undoes CSV-quoting artifacts (e.g. `""våtplate""`), and de-dupes
rows. The highest non-empty level in each row is its main term; lower levels are
its parents.

## Reviewing matches (`03b_review.csv`)

This is the human's main task. `review-export` writes one row per term that
needs a decision (everything not auto-accepted; add `--include-auto` to also see
auto-accepted rows and override them). Context columns show the machine's
proposal — `proposed_id`, `proposed_facet`, `matched_term`, `best_score`,
`reasons` (with runner-up candidates), and the structured `match_type`. The LLM
advisory predictions `expected_facet`/`expected_hierarchy` appear next to the
proposal for comparison.

Edit only these columns, then save as CSV:

| column | what to put |
|---|---|
| `accept` | `yes` to keep the match, `no`/blank to drop the term |
| `chosen_id` | a different authority id, if you're overriding the proposal |
| `chosen_target_term` | a corrected English/target label, if needed |
| `chosen_facet` | a corrected facet, if needed |
| `notes` | free text, carried into the output |

A term with `accept` blank or `no` is **excluded** from the final output. On
auto-accepted rows the `accept` cell is pre-filled `auto`; leaving it untouched
keeps the machine's decision, and the term stays counted as auto-accepted (only
an actual edit makes it a human review).

If you edit in Excel, save as **CSV UTF-8** — the file is read tolerantly
(UTF-8/BOM, Windows-1252, and `;` or `,` delimiters all work), but saving as
UTF-8 keeps Norwegian characters correct.

## Final output

`assemble` keeps every auto-accepted term plus every term you accepted in
review, and writes:

* **`04_final.json`** — one record per kept term. Alongside the source term and
  the chosen authority id/link/facet, each record carries provenance:
  `decision_source` (`auto_accept` | `human_review`), `match_type` (why it was
  tiered — e.g. `nb_exact`, `score_gap`), `matched_lang`, `translation_source`
  (`source_data` | `llm` | `human`), and `recommended_translation` /
  `recommended_authority` flags marking values worth writing back to MuseumPlus.
* **`04_final.csv`** — the same records flattened for Excel.
* **`04_linkedart.json`** — a Linked Art fragment per match, attached to the
  right slot (e.g. `made_of` on the object, `classified_as` on the production
  event) from the profile's facet → property map.
* **`log.txt`** — a run report: the decision-relevant config used, tier and
  auto-accept-basis distributions, the matched-language breakdown of the final
  records, review outcomes (accepted / rejected / undecided, and how often a
  reviewer overrode the proposal), translation provenance, and the no-match
  terms.

> If review/no-match terms exist but no edited `03b_review.csv` is present,
> `assemble` warns and keeps only the auto-accepted terms — run `review-export`,
> edit it, then re-run.

## Optional: machine-recommended English (the translate steps)

Many source terms have no English label. The `translate` step asks an LLM for a
recommended English term (using the Norwegian term plus its parent and sibling
context) so that `lookup` has an English query to try as well. It needs extra
dependencies and an API key:

```bash
pip install -e ".[llm]"
# PowerShell:  $env:ANTHROPIC_API_KEY = "sk-ant-..."
# bash:        export ANTHROPIC_API_KEY=sk-ant-...
```

It runs between `prep` and `lookup`, with a review gate:

```
translate        01_prepared.json                       -> 01b_translations.csv   (review/edit)
translate-apply  01_prepared.json + 01b_translations.csv -> 01b_translated.json
lookup --inp 01b_translated.json
```

In `01b_translations.csv`, `approved_english` is pre-filled with the suggestion
and `accept` with `yes`; edit `approved_english` to override, or clear `accept`
to skip a term. `translate-apply` folds approved English into the terms, tagged
`target_source = llm` (or `human` if you edited it). Remember: this English is an
**untrusted lookup query** — it can surface candidates but never auto-accepts.

The translate step also produces three optional, prunable extras that ride
through the same review gate:

| field | what it does | trust |
|---|---|---|
| `alternatives` | extra fallback search queries, tried only when the primary queries find nothing convincing | lookup query only — review, never auto-accept |
| `expected_facet` | the LLM's guess at the term's facet | advisory only — breaks ties between near-equal candidates and annotates review; never changes the verdict |
| `expected_hierarchy` | a finer guess, picked from the profile's `preferred_hierarchies` | advisory only — steers which candidate is proposed; never changes the verdict |

Helper commands:

```bash
# preview cost before spending: how many terms, ~how many API calls
museumvocab-reconcile --profile $P translate --dry-run
# pull LLM-flagged "looks misplaced in the hierarchy" rows out for cleanup
museumvocab-reconcile --profile $P flag-anomalies          # -> 01b_anomalies.csv
# re-translate just some rows (e.g. after a prompt change), preserving the rest
museumvocab-reconcile --profile $P retranslate --confidence low,medium
museumvocab-reconcile --profile $P retranslate --ids 100490880,100491296
```

The API key is read only from `ANTHROPIC_API_KEY`; never put it in a profile.

## Tuning lookup speed and throttling

`lookup` is the slow stage — for each term it fetches every candidate concept
and walks its hierarchy, so runtime and cache size scale with how many
candidates get enriched. The levers live in the profile's `lookup:` block and
each has a matching CLI flag:

* `enrich_top_n` (`--enrich-top-n`) — enrich at most N candidates per term,
  highest score first. The biggest single lever.
* `min_candidate_score` (`--min-score`) — drop weak candidates before enriching.
* `result_limit` (`--limit`) — candidates requested per query.

`lookup` is resumable: re-running skips finished terms. The cache (`cache.json`)
stores only compact per-concept data, so it stays small — keep it between runs.

**If many terms suddenly return 0 candidates, you are probably being rate-
limited.** Do *not* delete `cache.json` (that forces a full re-fetch and makes
it worse). Persistent failures are now recorded as `ERROR` entries rather than
silent zeros, and resume retries them; `classify` refuses to run while errors
remain, so they can't be mistaken for real no-matches. Slow down with
`--sleep 0.5 --request-delay 0.3`, reduce volume with a higher `--min-score` /
lower `--enrich-top-n`, and wait a few minutes. To re-attempt terms an older
build zeroed silently, delete `02_candidates.json` once (keep `cache.json`) and
re-run.

## Writing a profile

A profile is one YAML file describing how to reconcile one vocabulary against
one authority. Start from a bundled profile (e.g.
`museumvocab_reconcile/profiles/techniques.aat.yaml`) and adjust. The blocks
that matter most:

```yaml
profile: techniques            # a name for logs
authority: aat                 # aat | iconclass

languages:
  source: nb                   # the source vocabulary's main language
  target: en                   # the lookup/output language
  trusted_exact_match_langs: [nb, nn]   # exact match in these auto-accepts; [] for Iconclass
  # match_langs: [nb, nn, en]  # optional: require the matched label be in these languages

facets:
  accept_all: false            # true = accept any facet (ignores `accepted`)
  accepted: [techniques, work_types, materials, formats, design_motifs]
  linked_art_property:         # facet -> where its match attaches in Linked Art
    materials: {target: object, prop: made_of}
    techniques: {target: production, prop: classified_as}

thresholds:
  auto_accept:
    mode: full                 # full (exact OR score/gap) | exact_only | off (all -> review)
    min_score: 25
    min_score_gap: 5

source_schema:                 # how to read the export's columns
  id_field: ID
  status_field: Status
  include_status: [Gyldig]
  level_pattern: "Level_{n}_{lang}"   # tolerant of extra levels / other language codes
  dedupe_by: ID

review:
  include_auto_accepted: false # true also lists auto-accepted rows in the review CSV

# lookup: and translation: blocks tune speed and the optional LLM step;
# see a bundled profile for the full set of options, each documented inline.
```

The config knobs most worth understanding:
* `facets.accept_all` / `facets.accepted` — which authority facets count.
* `thresholds.auto_accept.mode` — `full` (exact or score/gap), `exact_only`
  (only a trusted exact match), or `off` (everything goes to review).
* `languages.trusted_exact_match_langs` — the languages in which an exact match
  is trusted enough to auto-accept (`[]` disables it, as for Iconclass).
* `facets.preferred_hierarchies` with `hierarchy_mode: prefer` — refines *which*
  candidate is proposed within the accepted facets; it never changes the accept
  gate. Discover anchor ids with `python tools/profile_hierarchies.py`.

## Authorities (adapters)

* **AAT** — reconciles via the Getty OpenRefine endpoint and reads each concept's
  JSON-LD. **As of 2024 Getty serves Linked Art JSON-LD, not SKOS**; the adapter
  parses the current Linked Art form (and still tolerates the older GVP/SKOS
  shape). A concept's facet is found by walking its `broader` chain to a known
  root. Concepts whose root isn't mapped get `facet: null` and route to review
  unless `accept_all` is set — extend the small `FACET_ROOTS` map at the top of
  `adapters/aat.py` for facets in other branches.
* **Iconclass** — per-notation JSON gives labels and the ancestor path; the
  search route is discovered from the live API spec at run time. Iconclass has
  no Norwegian, so its profile pivots on English
  (`trusted_exact_match_langs: [en]`).

## Helper tools

In `tools/` (run from your own machine — the Getty/Iconclass APIs are not
reachable from a sandbox):
* `diagnose_term.py` — run lookup + classify for a single term end to end, to
  see why it matched (or didn't).
* `profile_hierarchies.py` — explore the AAT hierarchy distribution in a lookup
  result to choose `preferred_hierarchies` anchors.
* `verify_facets.py` — check `FACET_ROOTS` ids against live Getty records.

## Tests

An offline test suite lives in `tests/` — no network, no Getty, no secrets;
fake responses and fixture concepts are injected. It covers the engine logic and
the known tripwires: confidence tiering and trusted-language (`nb`/`nn`) auto-
accept, the post-2024 Linked-Art parsing, facet resolution, the rate-limit rule
(a persistent failure must surface as `ERROR` and be retried, never logged as
`no_match`), and auto-accept provenance through the review round-trip.

```powershell
pip install pytest
python -m pytest -q
```
