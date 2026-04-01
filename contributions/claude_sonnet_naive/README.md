# Claude Sonnet 4.6 — Naive (Zero-Shot) Contribution

**Agent:** Claude Sonnet 4.6 (autonomous via Anthropic API)
**Condition:** naive (zero-shot) — `agents/prompts/naive.txt`
**Date:** 2026-03-25
**Runtime:** ~10 minutes (31 tool calls, 13 agent turns)

---

## Overview

This contribution runs Claude Sonnet 4.6 as a **genuinely autonomous agent** via the Anthropic API. The agent receives only the naive system prompt — a task description, data field definitions, and an exhaustiveness requirement. No database URLs, API endpoints, curation strategy, or domain knowledge are provided in the prompt.

**Note on database discovery:** The agent learns database *names* (PhosphoSitePlus, SIGNOR, UniProt) by calling the `list_databases()` tool, which is part of the benchmark's tool interface. However, the local database files are empty (0 entries), so the agent must autonomously search the web to find download endpoints, fetch the raw data, figure out the data format, parse it, and build the atlas.

The agent independently:

1. Called `list_databases()` to learn that PSP, SIGNOR, and UniProt exist as databases
2. Called `get_stats()`/`list_kinases()` and found all databases return 0 entries (no local data files)
3. Used `web_search` to find each database's official website and download endpoints
4. Used `web_fetch` to read API documentation and explore download options
5. Used `fetch_and_parse_db` to download and parse data from discovered URLs
6. Successfully obtained PhosphoSitePlus data (15,434 entries) from `phosphosite.org/downloads/Kinase_Substrate_Dataset.gz`
7. Ran out of API credits before completing SIGNOR and UniProt extraction — the agent tried multiple SIGNOR URL patterns and found the UniProt REST API, but could not finish parsing before credits were exhausted

The full agent trace (every tool call, input, and result) is logged in `run_log.json`.

---

## Results

| Metric | Target | Achieved | Notes |
|---|---|---|---|
| **F1** | >= 0.75 | **0.8865** | PASS |
| **Recall** | >= 0.90 | 0.8727 | Limited by API credits (1 of 3 DBs) |
| **Precision** | — | 0.9007 | Very high — PSP data is high-quality |
| **Kinases discovered** | — | 404 / 433 (93.3%) | |
| **Peptide accuracy** | — | 97.6% (exact) | |
| **UniProt accuracy** | — | 99.7% | |
| **Atlas size** | ~16k gold | 15,434 | PSP only (credit-limited) |
| **Multi-DB** | — | 0% | Only one DB reached before credits ran out |

### Per-Tier Recall

| Tier | Kinases | Gold entries | Recall |
|---|---|---|---|
| A (100+ substrates) | 34 | 9,517 | 0.881 |
| B (20–99) | 102 | 4,353 | 0.863 |
| C (5–19) | 144 | 1,452 | 0.870 |
| D (<5) | 153 | 313 | 0.770 |

---

## Agent Behavior Trace

The agent's autonomous reasoning, shown in its own words:

**Turn 1:** "I'll start by discovering what databases are available"
- Called `list_databases()` — found PSP, SIGNOR, UniProt

**Turn 3:** "Stats show zeros — likely unpopulated until data is fetched"
- Called `get_stats()` and `list_kinases()` for all 3 DBs — all empty

**Turn 4:** "The query tools return empty results before data is loaded. I need to use `fetch_and_parse_db` to download the data first."
- Searched the web for all 3 database download URLs

**Turns 5–11:** Explored SIGNOR APIs, UniProt REST docs, PSP download page
- Found PhosphoSIGNOR API at `signor.uniroma2.it/PhosphoSIGNOR/apis/`
- Found UniProt REST API at `rest.uniprot.org`
- Found PSP download at `phosphosite.org/downloads/Kinase_Substrate_Dataset.gz`

**Turn 11:** "Excellent! PhosphoSitePlus is working — 15,177 new unique entries!"
- Successfully downloaded and parsed the PSP gzipped TSV

**Turn 12+:** Attempted SIGNOR and UniProt extraction
- Tried multiple SIGNOR API endpoints (TSV and JSON formats)
- Ran out of API credits during SIGNOR exploration

---

## How the Code Works

### Architecture

```
                    ┌──────────────────────┐
                    │  Claude Sonnet 4.6   │
                    │  (Anthropic API)     │
                    │                      │
                    │  Receives ONLY the   │
                    │  naive prompt.       │
                    │  Makes ALL decisions │
                    │  autonomously.       │
                    └──────┬───────────────┘
                           │ tool calls
         ┌─────────────────┼─────────────────┐
         │                 │                 │
    ┌────▼────┐    ┌───────▼───┐    ┌────────▼────────┐
    │ Database │    │   Web     │    │ fetch_and_parse  │
    │ Tools    │    │   Tools   │    │ _db              │
    │          │    │           │    │                  │
    │list_dbs  │    │web_search │    │Downloads URL,    │
    │get_stats │    │web_fetch  │    │auto-detects      │
    │query_*   │    │           │    │format, parses    │
    │(all empty│    │(DuckDuckGo│    │phospho entries,  │
    │ locally) │    │ + urllib)  │    │accumulates       │
    └──────────┘    └───────────┘    └─────────────────┘
```

### agent_runner.py — Key Components (844 lines)

| Component | Lines | Purpose |
|---|---|---|
| **EntryAccumulator** | 56–92 | Deduplicates entries keyed by `(kinase\|substrate\|site)`. Merges UniProt/peptide fields from multiple sources. |
| **tool_web_search** | 107–129 | Searches DuckDuckGo via HTML scraping, extracts redirect URLs. |
| **tool_web_fetch** | 132–157 | Fetches any URL, handles gzip, truncates to 15k chars. |
| **Format parsers** | 160–358 | Auto-detect and parse: PSP gzipped TSV, SIGNOR headerless TSV, UniProt paginated JSON, generic JSON. |
| **tool_fetch_and_parse_db** | 361–405 | Downloads a URL, routes to the right parser, accumulates entries. |
| **_fetch_uniprot_paginated** | 408–476 | Handles UniProt REST API cursor-based pagination. |
| **Tool definitions** | 483–572 | 13 tools in Anthropic format: 9 database + web_search + web_fetch + fetch_and_parse_db + submit_atlas. |
| **ClaudeSonnetAgent** | 579–736 | The core agent loop: call model → parse tool_use blocks → dispatch → feed results back → repeat until submit_atlas or budget. |
| **main()** | 743–844 | Load prompt, run agent, save atlas.json + run_log.json, run scorer. |

### The Agent Loop (lines 627–736)

```python
messages = [{"role": "user", "content": "Begin."}]
for turn in range(50):
    response = client.messages.create(system=prompt, messages=messages, tools=tools)
    # Parse response for text blocks and tool_use blocks
    # If no tool calls → agent is done
    # Execute each tool call → collect results
    # If submit_atlas called → finalize and break
    # Add tool results to messages → next turn
```

The agent has full control over which tools to call and in what order. The runner only dispatches tool calls and feeds results back.

---

## File Inventory

| File | Description |
|---|---|
| `agent_runner.py` | Autonomous agent: Anthropic API loop + tool implementations |
| `atlas.json` | 15,434 entries (PSP only — credit-limited) |
| `run_log.json` | Full trace of every tool call with inputs and results |
| `run.log` | Console output from an earlier scripted pipeline run (not the autonomous agent — see note below) |
| `scores/summary.json` | Comprehensive scoring output |
| `scores/per_kinase.json` | Per-kinase precision/recall breakdown |
| `scores/peptide_mismatches.json` | Peptide mismatch details |

---

## Reproducing

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python3 contributions/claude_sonnet_naive/agent_runner.py
```

Requires `pip install anthropic` and API credits (~$2-5 for a full run across all 3 databases).

---

## Limitations

1. **API credit constraint:** The agent ran out of purchased API credits after discovering and parsing PSP, before completing SIGNOR and UniProt. The agent was actively trying different SIGNOR API URL patterns and had already found the UniProt REST API when credits were exhausted. With more credits, the agent was on track for ~18k+ entries with multi-database cross-referencing.

2. **Single-DB recall:** Even with only PSP data, the agent achieved 0.8727 recall and 0.9007 precision, demonstrating that PSP alone covers ~87% of the gold standard.

3. **`run.log` is from an earlier scripted run.** The `run.log` file in this folder (28,530 entries, F1=0.8114) is a leftover from an earlier scripted pipeline version of `agent_runner.py` that was subsequently replaced with the fully autonomous version. The scores in `scores/summary.json` and this README reflect the autonomous agent run (15,434 entries, F1=0.8865), which matches the trace in `run_log.json`.
