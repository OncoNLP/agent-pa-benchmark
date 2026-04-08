import os
import json
import time
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

class PhosphoAtlasAutonomousAgent:
    def __init__(self, api_key):
        """Initializes the agent with persistent memory to survive long-run truncation."""
        self.client = genai.Client(api_key=api_key)
        self.history = []
        
        # --- PERSISTENT STATE ---
        self.internal_atlas = [] 
        self.seen_triplets = set() 
        
        # --- METRICS & LOGGING ---
        self.start_time = time.time()
        self.tool_calls = 0
        self.db_hit_counts = {}

        # --- TOKEN TRACKING ---
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.per_turn_usage = []

    def _execute_http(self, call):
        """Live HTTP tool with dynamic source tracking, auto-retries, and safety guardrails."""
        self.tool_calls += 1
        args = call.args or {}
        url = args.get('url', 'UNKNOWN_URL')
        method = args.get('method', 'GET')
        
        if any(x in url.lower() for x in ["localhost", "127.0.0.1", "0.0.0.0"]):
            print(f"🛑 BLOCKED: Agent attempted local call to {url}")
            return {"error": "CRITICAL: No local database exists. You must use public REST APIs."}
        
        domain = "OTHER_API"
        domain_match = re.search(r'https?://(?:www\.)?([^/]+)', url)
        if domain_match:
            domain = domain_match.group(1).upper()
        
        self.db_hit_counts[domain] = self.db_hit_counts.get(domain, 0) + 1
        print(f"📡 API CALL [{self.tool_calls}]: {method} -> {url[:60]}...")
        
        session = requests.Session()
        retries = Retry(total=3, backoff_factor=1.5, status_forcelist=[429, 500, 502, 503, 504])
        session.mount("https://", HTTPAdapter(max_retries=retries))

        try:
            params = args.get('params') or {}
            max_payload_chars = 150000
            target_payload_chars = 120000

            def _extract_list_payload(payload):
                if isinstance(payload, list):
                    return "root", None, payload
                if isinstance(payload, dict):
                    for key in ("results", "data", "items"):
                        value = payload.get(key)
                        if isinstance(value, list):
                            return "dict", key, value
                return None, None, None

            def _truncate_items(items):
                kept = []
                for item in items:
                    kept.append(item)
                    if len(json.dumps(kept)) > target_payload_chars:
                        kept.pop()
                        break
                return kept

            def _rebuild_payload(container_kind, key, original_payload, items):
                if container_kind == "root":
                    return items
                rebuilt = dict(original_payload)
                rebuilt[key] = items
                return rebuilt

            res = session.request(
                method=method, 
                url=url, 
                params=params, 
                json=args.get('data'), 
                timeout=(10, 45)
            )
            res.raise_for_status()
            data = res.json()

            payload_size = len(json.dumps(data))
            if payload_size > max_payload_chars:
                print(f"⚠️ PAYLOAD BLOCKED: {payload_size} chars. Attempting automatic downscoping.")

                # Try API-agnostic pagination for oversized GET requests.
                if method == "GET" and isinstance(params, dict):
                    base_page_size = params.get("limit", params.get("per_page", params.get("page_size", 200)))
                    max_pages_raw = params.get("_max_pages", 25)
                    try:
                        page_size = max(1, min(int(base_page_size), 2000))
                    except Exception:
                        page_size = 200
                    try:
                        max_pages = max(1, min(int(max_pages_raw), 200))
                    except Exception:
                        max_pages = 25

                    use_page_style = any(k in params for k in ("page", "per_page", "page_size"))
                    try:
                        current_page = max(1, int(params.get("page", 1)))
                    except Exception:
                        current_page = 1
                    try:
                        current_offset = max(0, int(params.get("offset", 0)))
                    except Exception:
                        current_offset = 0

                    aggregated_items = []
                    pages_fetched = 0
                    has_more = False
                    next_hint = None
                    previous_signature = None
                    paged_status = res.status_code

                    for _ in range(max_pages):
                        page_params = dict(params)
                        page_params.pop("_max_pages", None)
                        if use_page_style:
                            page_params["page"] = current_page
                            if "per_page" in page_params:
                                page_params["per_page"] = page_size
                            else:
                                page_params["page_size"] = page_size
                        else:
                            page_params["limit"] = page_size
                            page_params["offset"] = current_offset

                        page_res = session.request(
                            method=method,
                            url=url,
                            params=page_params,
                            timeout=(10, 45)
                        )
                        page_res.raise_for_status()
                        page_data = page_res.json()
                        paged_status = page_res.status_code

                        container_kind, list_key, page_items = _extract_list_payload(page_data)
                        if page_items is None:
                            break
                        if not page_items:
                            break

                        signature = f"{len(page_items)}:{json.dumps(page_items[:2], sort_keys=True)}"
                        if previous_signature == signature:
                            has_more = True
                            next_hint = {"reason": "pagination_not_honored"}
                            break
                        previous_signature = signature

                        old_len = len(aggregated_items)
                        aggregated_items.extend(page_items)
                        if len(json.dumps(aggregated_items)) > target_payload_chars:
                            del aggregated_items[old_len:]
                            has_more = True
                            if use_page_style:
                                next_hint = {"page": current_page}
                            else:
                                next_hint = {"offset": current_offset}
                            break

                        pages_fetched += 1
                        fetched_count = len(page_items)
                        if fetched_count < page_size:
                            break

                        if use_page_style:
                            current_page += 1
                        else:
                            current_offset += fetched_count

                    if pages_fetched == max_pages:
                        has_more = True
                        if use_page_style:
                            next_hint = {"page": current_page}
                        else:
                            next_hint = {"offset": current_offset}

                    if aggregated_items:
                        print(f"⚠️ Returning partial paginated data: {len(aggregated_items)} rows.")
                        return {
                            "status": paged_status,
                            "data": aggregated_items,
                            "pagination": {
                                "auto_paginated": True,
                                "page_size": page_size,
                                "pages_fetched": pages_fetched,
                                "has_more": has_more,
                                "next": next_hint,
                            },
                        }

                # Universal fallback: truncate list-like payloads to fit model context budget.
                container_kind, list_key, list_items = _extract_list_payload(data)
                if list_items is not None:
                    truncated_items = _truncate_items(list_items)
                    if truncated_items:
                        truncated_payload = _rebuild_payload(container_kind, list_key, data, truncated_items)
                        return {
                            "status": res.status_code,
                            "data": truncated_payload,
                            "pagination": {
                                "truncated": True,
                                "original_count": len(list_items),
                                "returned_count": len(truncated_items),
                                "has_more": len(truncated_items) < len(list_items),
                            },
                        }

                return {"error": "PAYLOAD_TOO_LARGE: Response too big for memory and could not be safely downscoped."}
            
            return {"status": res.status_code, "data": data}
        except Exception as e:
            return {"error": f"Request failed: {str(e)}"}

    def _save_curated_data(self, call):
        """Tool 2: The Agent's active 'Save Button' to persist findings to atlas.json."""
        args = call.args or {}
        k = args.get("kinase_gene", "Unknown").strip().upper()
        s = args.get("substrate_gene", "Unknown").strip().upper()
        p = args.get("phospho_site", "Unknown").strip()
        u = args.get("substrate_uniprot", "Unknown").strip()
        pep = args.get("heptameric_peptide", "Unknown").strip()
        db = args.get("source_database", "Unknown").strip()

        triplet_key = f"{k}-{s}-{p}".upper()
        
        if k == "UNKNOWN" or s == "UNKNOWN":
            return {"status": "error", "message": "Kinase and Substrate are required."}

        if triplet_key not in self.seen_triplets:
            self.seen_triplets.add(triplet_key)
            self.internal_atlas.append({
                "kinase_gene": k,
                "substrate_gene": s,
                "phospho_site": p,
                "substrate_uniprot": u,
                "heptameric_peptide": pep,
                "supporting_databases": [db]
            })
            print(f"💾 SAVED BY AGENT: {k} -> {s} ({p}) [Total archive: {len(self.internal_atlas)}]")
            return {"status": "success", "message": f"Successfully saved {triplet_key} to atlas."}
        else:
            # If already exists, append the new database source
            for entry in self.internal_atlas:
                if f"{entry['kinase_gene']}-{entry['substrate_gene']}-{entry['phospho_site']}".upper() == triplet_key:
                    if db not in entry["supporting_databases"]:
                        entry["supporting_databases"].append(db)
            return {"status": "ignored", "message": f"{triplet_key} already exists, updated sources."}

    def run(self, mission_prompt):
        """Main autonomous loop with strict turn order and background state injection."""
        tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name="http_request",
                description="Query a public biological REST API.",
                parameters={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"}, 
                        "method": {"type": "string", "enum": ["GET", "POST"]},
                        "params": {"type": "object"}
                    },
                    "required": ["url", "method"]
                }
            ),
            types.FunctionDeclaration(
                name="save_curated_data",
                description="CRITICAL: Use this tool to save the valid kinase-substrate relationships you discover into the final JSON atlas.",
                parameters={
                    "type": "object",
                    "properties": {
                        "kinase_gene": {"type": "string"},
                        "substrate_gene": {"type": "string"},
                        "phospho_site": {"type": "string"},
                        "substrate_uniprot": {"type": "string"},
                        "heptameric_peptide": {"type": "string"},
                        "source_database": {"type": "string"}
                    },
                    "required": ["kinase_gene", "substrate_gene", "phospho_site", "source_database"]
                }
            )
        ])]

        self.history = [{"role": "user", "parts": [{"text": "Proceed with the curation task."}]}]
        MAX_TURNS = 1000
        MAX_MINUTES = 60
        turn_count = 0

        while turn_count < MAX_TURNS:
            turn_count += 1
            elapsed_m = (time.time() - self.start_time) / 60
            if elapsed_m > MAX_MINUTES:
                print(f"⏱️ Runtime limit reached ({MAX_MINUTES}m).")
                break

            if len(self.history) > 15:
                print("🧹 Cleaning history for token safety...")
                self.history = [self.history[0]] + self.history[-6:]

            current_instr = mission_prompt + f"\n\nCURRENT PROGRESS: You have already archived {len(self.internal_atlas)} kinase-substrate pairs into persistent memory. Keep querying and use save_curated_data to store new findings!"

            try:
                response = self.client.models.generate_content(
                    model="gemini-3.1-pro-preview", 
                    contents=self.history,
                    config=types.GenerateContentConfig(
                        system_instruction=current_instr,
                        tools=tools,
                        thinking_config=types.ThinkingConfig(include_thoughts=True),
                    )
                )

                if not response.candidates or not response.candidates[0].content:
                    break

                parts = response.candidates[0].content.parts or []
                if not parts:
                    print("⚠️ Model returned empty content parts; ending loop safely.")
                    break

                # Track token usage from Gemini response
                um = getattr(response, "usage_metadata", None)
                if um:
                    inp = getattr(um, "prompt_token_count", 0) or 0
                    out = getattr(um, "candidates_token_count", 0) or 0
                    self.total_input_tokens += inp
                    self.total_output_tokens += out
                    self.per_turn_usage.append({"turn": turn_count, "input_tokens": inp, "output_tokens": out})
                    print(f"📊 Tokens: in={inp:,} out={out:,} | total={self.total_input_tokens+self.total_output_tokens:,}")
                
                for part in parts:
                    if getattr(part, 'thought', False) and part.text:
                        print(f"🧠 {part.text.strip().replace('\n', ' ')[:90]}...")

                self.history.append(response.candidates[0].content)
                
                tool_parts = []
                for part in parts:
                    if part.function_call:
                        # Route the tool to the correct Python function
                        if part.function_call.name == "http_request":
                            res = self._execute_http(part.function_call)
                        elif part.function_call.name == "save_curated_data":
                            res = self._save_curated_data(part.function_call)
                        else:
                            res = {"error": "Unknown function"}

                        tool_parts.append({
                            "function_response": {
                                "name": part.function_call.name, 
                                "response": res, 
                                "id": part.function_call.id
                            }
                        })

                if not tool_parts:
                    print("🏁 Curation mission concluded by agent.")
                    break

                self.history.append({"role": "user", "parts": tool_parts})
            
            except Exception as e:
                print(f"❌ Error in main loop: {e}")
                break

        return self.internal_atlas

if __name__ == "__main__":
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("❌ ERROR: GOOGLE_API_KEY not found in environment.")
        exit(1)

    agent = PhosphoAtlasAutonomousAgent(api_key)
    
    prompt = """You are a bioinformatics researcher tasked with building a comprehensive human protein phosphorylation atlas from available databases.
            Your goal: Curate ALL known human kinase-substrate-phosphosite relationships by systematically querying the databases available to you.
            For each relationship, you must capture:
              - Kinase gene symbol (the enzyme)
              - Substrate gene symbol (the target protein)
              - Phosphorylation site (e.g., Y15, S10, T161) - Heptameric peptide sequence 
                around the site (if available) - Substrate UniProt accession (if available)
              - Which database(s) support this relationship. Fill out the form using the xlsx format shown under the sample_PA2.xlsx.
            Requirements: 
            1. Be EXHAUSTIVE — the atlas should contain every kinase-substrate-site triplet present in the databases. Missing entries is worse than having extra entries. 
            2. Cross-reference across databases — if the same relationship appears in multiple databases, record all supporting sources.
            3. Do NOT fabricate data. Only include relationships returned 
            by the tools. Start by discovering what databases are available, then develop and execute a systematic curation strategy.
            CRITICAL: You MUST use the `save_curated_data` tool to explicitly save every relationship you find into the final JSON atlas."""
    
    run_started_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"Starting Persistent State Run at {run_started_at}")
    results = agent.run(prompt)

    elapsed = time.time() - agent.start_time
    run_finished_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    with open("atlas.json", "w") as f:
        json.dump(results, f, indent=2)

    # Save run_log with token usage (Gemini is subscription-based for most plans)
    run_log = {
        "agent": "Gemini 3.1 Pro",
        "model": "gemini-3.1-pro-preview",
        "condition": "naive",
        "tool_calls": agent.tool_calls,
        "elapsed_seconds": round(elapsed, 1),
        "atlas_size": len(results),
        "db_hit_counts": agent.db_hit_counts,
        "token_usage": {
            "total_input_tokens": agent.total_input_tokens,
            "total_output_tokens": agent.total_output_tokens,
            "total_tokens": agent.total_input_tokens + agent.total_output_tokens,
            "api_calls": len(agent.per_turn_usage),
            "per_call": agent.per_turn_usage,
            "note": "Gemini is subscription-based; token counts from usage_metadata for reference",
        },
        "metadata": {
            "agent_mode": "Gemini 3.1-Pro (Active Save Mode)",
            "started_at": run_started_at,
            "finished_at": run_finished_at,
            "runtime_min": round((time.time() - agent.start_time) / 60, 2),
        },
        "stats": {
            "total_curated": len(results),
            "tool_calls": agent.tool_calls,
            "sources_identified": sorted(list(agent.db_hit_counts.keys())),
            "hit_breakdown": agent.db_hit_counts,
        },
    }
    with open("run_log.json", "w") as f:
        json.dump(run_log, f, indent=2)

    print(f"\n📊 Token Summary: {agent.total_input_tokens+agent.total_output_tokens:,} total "
          f"(in={agent.total_input_tokens:,}, out={agent.total_output_tokens:,})")
    print(f"📁 Saved atlas.json ({len(results)} entries) and run_log.json")

    print(f"✅ COMPLETED. Saved {len(results)} entries to atlas.json.")