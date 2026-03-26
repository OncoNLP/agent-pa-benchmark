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
