# Agent Observation Log
Discussion points and behavioral findings for Paper 1.

---

## Qwen3-235B-A22B-Instruct-2507 (Together AI)

### DATE: 03_25_2025
### Finding 1: Qwen produces empty atlas with naive prompt and empty local database files
With no local database files present (default repo state), all tool calls
return empty results. The model recognized the failure and attempted fallback
strategies (keyword searches, querying known genes by name) but ultimately
submitted an empty atlas (atlas.json = []). This is not a model capability
finding — it reflects a misconfigured environment. The intended setup requires
either local database files (as Hui runs on her machine) or a live API backend.

**Resolution:** LiveDatabaseTools was built as a live UniProt REST API backend
and confirmed valid — equivalent to the local file approach other agents use.
This is the official Paper 1 data layer for our Qwen runs.

### Finding 2: Qwen does not autonomously discover external database APIs
When given only the standard tool interface with empty backends, Qwen stayed
within the provided tools and gave up when they returned nothing. It did not
attempt to identify or call external database URLs on its own. This contrasts
with Borna's Mistral setup which used generic HTTP tools — Mistral at least
attempted to find APIs, though it provided incorrect URLs (likely due to
training data cutoff). Qwen's failure mode: silence. Mistral's: wrong answer.
Worth noting as a behavioral difference in the discussion section.

### Finding 3: Tool call ordering — sequential, UniProt first
The agent loop is single-threaded. Qwen queries one database at a time and
decides the order itself. In both smoke test runs, it consistently chose
UniProt before SIGNOR. This is model-driven prioritization, not enforced by
the framework. Worth noting in methods: results are not parallelized.

### Finding 4 (explicit_prompt run, 03_25_2025): Qwen checks local tools before HTTP
Even with the explicit prompt injecting UniProt/SIGNOR URLs, Qwen called
list_databases (tool 1) and get_stats (tool 2) before attempting any HTTP
requests. This shows the model prefers to probe the provided tool interface
first. The local tools returned 0 entries, which then triggered HTTP attempts.
Behavioral pattern: "check what I have, then go external."

### Finding 5 (explicit_prompt run, 03_25_2025): Wrong UniProt ft_mod_res query term
Qwen queried ft_mod_res:phospho, which returns empty results — "phospho" is
not a valid UniProt ft_mod_res keyword. Valid terms are specific modification
names (e.g., Phosphoserine) or kinase names (e.g., CDK1). The model guessed
a plausible but incorrect term. After getting empty results it retried without
the ft_mod_res filter and received real data, but then fell into text output
mode rather than continuing as structured tool calls. SIGNOR was never reached.

**Stall note:** Cursor pagination on tool 4 hit a UniProt 500 error and stalled
for ~135s before the model recovered by retrying without the cursor.

### Design note: Accumulator extended to capture http_get responses directly
The base QwenAgent accumulator only captures entries from query_by_kinase /
query_by_substrate tool calls. For the HTTP tool experiment the model never
calls those — it calls http_get instead, returning raw JSON/TSV. Since the
model may hit the tool call budget before calling submit_atlas, we extended
the accumulator into _dispatch_http_get: UniProt responses are parsed on the
fly (ft_mod_res:KINASE queries only) and SIGNOR TSV is parsed in full. Entries
are deduplicated and stored in self._accumulated_entries as they arrive,
exactly like the base pattern. The model receives a short summary for SIGNOR
and a 2000-char truncated body for UniProt (down from 8000) to reduce context
bloat. This was necessary because atlas=0 even after 20 tool calls with real
data coming back — the entries simply were never captured.

### Finding 6 (explicit_prompt run, 03_25_2025): Accidental full run — SIGNOR in 1 call
What was supposed to be a 20-call smoke test turned into a complete SIGNOR
pull. The model called https://signor.uniroma2.it/API/getHumanData.php on
tool call 1, the accumulator parsed the full TSV dump (9671 entries), and the
run was effectively over. The model then fell into text mode trying to enumerate
human genes for UniProt before ever reaching ft_mod_res:KINASE queries.

Result from SIGNOR alone (3 tool calls, 141 seconds):

| Metric        | Value        |
|---------------|--------------|
| Atlas size    | 9671         |
| Recall        | 0.4629       |
| Precision     | 0.7483       |
| F1            | 0.572        |
| Kinases found | 377 / 433    |
| Multi-DB      | 0% (UniProt never reached) |
| Peptide acc.  | 0.2185       |
| TP / FP / FN  | 7237 / 2434 / 8398 |

This is the SIGNOR-only baseline. UniProt coverage is the remaining gap
(FN=8398 entries in gold not captured). Adding ft_mod_res:KINASE UniProt
queries on top should push recall higher.

### TODO: HTTP tool + URL injection (tentative Paper 1 approach)
Per Hui's guidance, injecting UniProt/SIGNOR API URLs is sanctioned for
Paper 1. Qwen needs an HTTP GET tool to actually use those URLs — without
it, URL injection is just noise.

- Add http_get tool to QwenAgent (contributions only, not shared infra)
- Write explicit_prompt.txt injecting UniProt + SIGNOR endpoints
- Add $50 token cost checkpoint per Hui's guidance
- Run and log: does Qwen navigate real APIs correctly? Does it paginate?
  Does it parse responses into the right atlas structure?
- PSP excluded — no public API
- LiveDatabaseTools retained for reference/fallback only

---

### Prompt fix: explicit_prompt v2 (03/30/2026)
Previous run stalled after SIGNOR dump — Qwen spiraled into text mode
trying to enumerate all human genes before doing UniProt queries.

Fix: restructured prompt into explicit Step 1 (SIGNOR) → Step 2 (UniProt).
Now tells the model to use SIGNOR's ENTITYA kinase names directly as the
UniProt ft_mod_res query list. Added hardcoded supplemental kinase list
(AKT1, MTOR, TP53, BRCA1, etc.) for kinases not in SIGNOR.

Runner output redirected: results/explicit_prompt/atlas.json (was qwen_prompt_testing/)

### Results folder structure (03/30/2026)
Reorganized outputs into results/ to match Hui's folder convention:
  results/naive/               ← empty atlas baseline
  results/explicit_prompt/     ← _signor_only files = accidental run (F1=0.572)
                                  atlas.json = full run (pending this session)
  results/paper_informed/      ← pending
  results/pipeline_informed/   ← pending
paper/                         ← drop PhosphoAtlas PDF + supplement here

### Finding 7: Explicit prompt full run results (03/30/2026)
75 tool calls, ~14 min, ~$1.89. SIGNOR (tool 1, 9671 entries) + 74 kinase-by-kinase
UniProt ft_mod_res queries. Model timed out after exhausting hardcoded kinase list
(MARK3 was last). Fallback accumulator saved all entries.

| Metric        | SIGNOR-only baseline | Explicit prompt (full) |
|---------------|----------------------|------------------------|
| Atlas size    | 9671                 | 10844                  |
| Recall        | 0.4629               | 0.473                  |
| Precision     | 0.7483               | 0.7235                 |
| F1            | 0.572                | 0.572                  |
| Kinases found | 377/433              | 377/433                |
| Multi-DB      | 0%                   | 0%                     |
| TP/FP/FN      | 7237/2434/8398       | 7396/2826/8239         |

UniProt added 159 new TPs. F1 unchanged because precision dropped slightly
(more FPs) while recall improved marginally. Multi-DB stays 0% — accumulator
deduplicates by triplet but doesn't merge supporting_databases across HTTP calls.

Note: SIGNOR API intermittently returns 2 rows under load. Added 3x retry
with 3s sleep in _dispatch_http_get. Background retry run needed 2 retries
before getting full 39643-row response.

### TODO: Next runs (by Tuesday EOD)
- [x] explicit_prompt full run → score ✓ (F1=0.572, 10844 entries)
- [x] paper_informed: get PhosphoAtlas PDF/supplement → build prompt → run → score ✓
- [x] pipeline_informed: adapt pipeline_guided.txt for Qwen + HTTP tools → run → score ✓

---

### Finding 8: paper_informed falls into text mode after SIGNOR (03/31/2026)
Despite receiving full paper methodology context (Olow et al. 2016 + supplement),
Qwen dropped into text mode after SIGNOR tool 1. Only 1 structured tool call fired.
UniProt CDK1 query was recovered via XML fallback but loop terminated immediately.
Result: SIGNOR-only atlas (9,671 entries), identical to accidental run baseline.
Cost: ~$0.01 (essentially free — never got past SIGNOR).

| Metric        | Value  |
|---------------|--------|
| Atlas size    | 9,671  |
| F1            | 0.572  |
| Recall        | 0.4629 |
| Precision     | 0.7483 |
| Kinases found | 377/433 |
| Multi-DB      | 0%     |
| Tool calls    | 1      |
| Cost          | ~$0.01 |

### Finding 9: pipeline_informed stays structured but loops redundantly (03/31/2026)
Step-by-step pipeline phases kept Qwen in structured tool-call mode for all 222 calls —
no text-mode drift. However, after exhausting the kinase list (~tool 75), model looped
back through the supplemental kinase list repeatedly, firing redundant UniProt queries
returning 0 new entries until the 60-min timeout.

Final result matches explicit_prompt exactly (same 10,844 entries, same F1=0.572),
but cost was ~$10.41 vs ~$1.89 for explicit_prompt — 5.5x more expensive for same output.
Peptide accuracy slightly lower than explicit_prompt (0.1961 vs 0.2185).

| Metric        | Value  |
|---------------|--------|
| Atlas size    | 10,844 |
| F1            | 0.572  |
| Recall        | 0.473  |
| Precision     | 0.7235 |
| Kinases found | 377/433 |
| Multi-DB      | 0%     |
| Tool calls    | 222    |
| Cost          | ~$10.41 |

### All Qwen3-235B conditions summary (03/31/2026)

| Condition         | Atlas  | F1    | Recall | Precision | Kinases  | Multi-DB | Cost    |
|-------------------|--------|-------|--------|-----------|----------|----------|---------|
| naive             | 0      | —     | —      | —         | —        | —        | ~$0     |
| explicit_prompt   | 10,844 | 0.572 | 0.473  | 0.7235    | 377/433  | 0%       | ~$1.89  |
| paper_informed    | 9,671  | 0.572 | 0.4629 | 0.7483    | 377/433  | 0%       | ~$0.01  |
| pipeline_informed | 10,844 | 0.572 | 0.473  | 0.7235    | 377/433  | 0%       | ~$10.41 |

Key finding: F1 is 0.572 across all non-naive conditions. Prompt structure affects
execution behavior (text-mode vs structured, cost) but not final F1 given our data
ceiling of SIGNOR + UniProt only (no PSP access). Hard ceiling is ~F1=0.572 without PSP.

### Finding 11: pipeline_informed v2 — PSP+SIGNOR, Together AI 500 (04/07/2026)
Two Together AI server errors today (503 then 500). Both times the run died after
exactly 2 tool calls (PSP + SIGNOR), before UniProt queries started. Fallback
accumulator saved 24,818 entries each time — identical to paper_informed Run 1.

| Metric        | Previous (03/31) | This run (04/07) |
|---------------|-----------------|------------------|
| Atlas size    | 10,844          | 24,818           |
| F1            | 0.572           | **0.869**        |
| Recall        | 0.473           | 0.952            |
| Precision     | 0.724           | 0.799            |
| Kinases found | 377/433         | 417/433          |
| Multi-DB      | 0%              | 0% (see note)    |
| Peptide acc.  | 0.196           | 0.980            |
| Tool calls    | 222             | 2 (cut by 500)   |
| Cost          | ~$10.41         | ~$0.01           |

Multi-DB 0% despite PSP+SIGNOR both present — suggests PSP and SIGNOR use
different gene symbol or site notation conventions for overlapping relationships,
so (kinase, substrate, site) keys don't match between them. The 2.6% Multi-DB
in paper_informed came from UniProt cross-referencing PSP entries, not PSP+SIGNOR
overlap. Worth investigating for the paper — which kinases overlap between DBs?

Note: if Together AI stabilizes, a clean full run would add UniProt queries
on top (same as paper_informed Run 2) and likely push Multi-DB slightly higher.

### Finding 12: pipeline_informed v3 — full token tracking confirmed (04/07/2026)
Re-ran pipeline_informed to get proper token_usage (previous run 500'd after 1 API call,
logging only $0.003). This run completed 80 tool calls (~798s) before a request timeout.
PSP+SIGNOR in first 2 calls (24,818 entries) as before; UniProt queries fired (tools 3–80)
but added no new entries. Fallback accumulator saved same 24,818 entries. Results identical.

| Metric        | Previous (04/07, 500'd) | This run (04/07, timeout) |
|---------------|------------------------|---------------------------|
| Atlas size    | 24,818                 | 24,818                    |
| F1            | 0.869                  | 0.869                     |
| Recall        | 0.952                  | 0.952                     |
| Precision     | 0.799                  | 0.799                     |
| Kinases found | 417/433                | 417/433                   |
| Peptide acc.  | 0.980                  | 0.979                     |
| api_calls     | 1 (incomplete)         | **17**                    |
| Cost          | $0.003 (incomplete)    | **$0.36**                 |

Token tracking is now complete and accurate. Run log has proper token_usage block.

### TODO: Remaining
- [ ] Build aggregate_scores.py to read all summary.json files into one table
- [ ] Investigate Multi-DB=0% for PSP+SIGNOR — are gene/site notations mismatched?
- [ ] Write paper section: discuss PSP gap, HTTP tool as Paper 1 contribution, prompt structure findings
- [ ] Clarify results/naive/atlas.json — 379 entries (expected empty), needs investigation

---

## Re-run after Hui's refactor (04/07/2026)

### Background
Hui pushed a code refactor (commits 906864f, 4d77ccc, 7aec32c) with three changes
relevant to us:
  1. scorer.py: peptide accuracy is now case-insensitive by default (exact + case-diff
     counted together). Old metric was peptide_close_accuracy; new primary metric is
     peptide_accuracy. Our previous numbers (0.2185, 0.1961) used the "close" metric
     so they should be consistent, but re-scoring with the new scorer is required.
  2. paper_informed prompt: PSP download URL added
     (http://phosphosite.org/downloads/Kinase_Substrate_Dataset.gz).
  3. live_runner.py: --all flag added for running all conditions in one shot.

Hui asked all contributors to re-run naive, paper_informed, and pipeline_guided
following the latest README. Old results archived to results/_archive_03_31/.

### PSP discovery (04/07/2026)
The PSP URL Hui added is a **direct public download** — no login required, HTTP 200,
760KB gzipped TSV (last updated 2026-03-17). We confirmed this via curl HEAD request.

This is significant: our F1=0.572 ceiling was entirely due to SIGNOR+UniProt only.
PSP is the primary source in the gold standard. Claude Opus had PSP access via local
database files (Hui populated databases/psp/ on her machine). We didn't, which is
why our recall was capped.

Fix: added gzip decompression + PSP TSV parser to _dispatch_http_get in
agent_with_http.py. Detects "phosphosite" in URL, decompresses resp.content with
gzip, parses human-only entries (SUB_ORGANISM == "human"), uppercases gene symbols
to HGNC convention. Paper_informed_prompt.txt updated: PSP is now Step 1,
SIGNOR Step 2, UniProt Step 3.

Smoke test (5 tool calls) confirmed:
  - Tool 1: PSP → 15,149 entries accumulated instantly
  - Tool 2: SIGNOR → 9,669 new entries on top (24,818 total)
  - Tool 3 (cut by budget): Qwen was already querying UniProt ft_mod_res:CDK1
  - Multi-DB > 0% expected once deduplication merges PSP+SIGNOR overlaps

### Finding 10: paper_informed v2 — PSP unlocks the ceiling (04/07/2026)

**Run 1 (PSP + SIGNOR only, Multi-DB bug):** 2 tool calls, 34s, ~$0.01.
Qwen followed the prompt: PSP (tool 1) → SIGNOR (tool 2) → dropped into
text-mode before UniProt. Fallback accumulator saved 24,818 entries.
Multi-DB was 0% due to bug: accumulator discarded duplicate triplets instead
of merging supporting_databases across sources.

**Run 2 (Multi-DB fix + full UniProt):** 70 tool calls, 11 min, ~$0.41.
After fixing _accumulate_http_entries to merge supporting_databases on
duplicates, Qwen ran all 3 steps: PSP → SIGNOR → 68 UniProt kinase queries.
UniProt added 471 new entries (25,289 total). Multi-DB now 2.6%.

| Metric        | Previous (03/31) | Run 1 (PSP+SIGNOR) | Run 2 (+ UniProt) |
|---------------|-----------------|---------------------|-------------------|
| Atlas size    | 9,671           | 24,818              | 25,289            |
| F1            | 0.572           | 0.8691              | **0.8606**        |
| Recall        | 0.4629          | 0.9522              | **0.9524**        |
| Precision     | 0.7483          | 0.7994              | 0.7849            |
| Kinases found | 377/433         | 417/433             | 417/433           |
| Multi-DB      | 0%              | 0%                  | **2.6%**          |
| Peptide acc.  | 0.2185          | 0.9794              | 0.9764            |
| Tool calls    | 1               | 2                   | 70                |
| Cost          | ~$0.01          | ~$0.01              | ~$0.41            |

Note: F1 dropped slightly (0.869 → 0.861) in Run 2 because UniProt added
471 new entries with lower precision (more FPs: 3736 → 4082) while recall
barely moved (0.9522 → 0.9524). UniProt's marginal contribution is small
given PSP already covers most of the gold standard.

**Tier breakdown (Run 2):**
  Tier A (34 kinases):  recall=0.9613
  Tier B (102 kinases): recall=0.9398
  Tier C (144 kinases): recall=0.9449
  Tier D (153 kinases): recall=0.8914  ← rare kinases still hardest

**Key finding:** PSP was the missing piece. F1 ceiling of 0.572 was entirely
due to lack of PSP access. Adding PSP in one http_get call pushed F1 to 0.869
and recall to 0.952. This directly answers the paper's PSP gap analysis.
Multi-DB fix confirmed working (0% → 2.6%). UniProt adds marginal recall but
hurts precision — PSP+SIGNOR is the sweet spot for this model.
