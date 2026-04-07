1. Mistral by Borna: 

Naive:

I previously went past zero shot and prompt engineered the model to try continuously at obtaining databases online and not stopping until it succeeds. I do not have the exact log of what it tried, but these were the websites tried and reasons for failure: 

- PhosphoSitePlus — requires a license/login for bulk downloads. The "ok"     
responses are just HTML login pages, not data.
- HPRD — completely down (503 on every request). The project has been defunct 
for years.                                                                    
- PhosphoELM — all download links 404. Also defunct.
- phosphonet, ptmcos, kinase.com — all down or no public API.                 
- SIGNOR — the API exists but it's returning empty ([], 16 chars). The model  
isn't hitting the right parameters.                                           
- UniProt — works but the model has the wrong base URL.        

After that, I removed the prompt I injected on top of the zero shot prompt and only gave the model HTTP-get tools and allowed it to decide for itself. The scores can be found in the mistral_large/scores/ directory for the actual zero-shot attempt.

The naive zero shot run resulted in a response of 15 kinases. I am not sure why the agent ran only 15 sources from the sites. If it is implying that it had access to the sites, why are there only 15 kinases? Are they possibly hallucinated?

Sites listed as sources in the run log were: PhosphoSitePlus, SIGNOR, ELM, and IntAct. However, only PSP and SIGNOR were listed as sources.

It also says that it used UniProt for the heptameric peptide sequence when it was unavailable, which makes me wonder why it didn't use UniProt anyways.
Also, yesterday when I was trying the model before I knew that we needed to log failures, this model was not able to access UniProt because it had the wrong URL.
This makes me think it may be hallucinating.

Todo: check for hallucinations, make it grab more kinases, give URL endpoints.

Paper informed: 

The first run attempt failed on turn 3 with a 503 "unreachable_backend" error from Mistral's API, likely caused by the context size (combined OCR text was ~124k characters before any convo or tool results were added).

To address this, added 2 fixes: (1) retry logic with exponential backoff (up to 5 retries for transient API errors) and (2) truncation caps on the OCR text to drop the initial context window size about in half. This run completed successfully but only ran 4 turns before submitting. It only hit phosphosite at different URL endpoints. This is almost certainly because phosphositeplus requires a license for downloads, so the agent was definitely just getting HTML errors. The 5 results may just be hallucinations based on that fact. 

The model did not try SIGNOR, UniProt or any of the other dbs mentioned in the paper it was given.

# RERUNS WITH UPDATED SCORER:

Re-ran everything on 2026-04-07 with the new scorer. Honestly the results are pretty rough across the board.

Naive gave me 10 entries, recall basically zero (0.0005), F1 0.001. Same story as last time — the model spent 4 turns poking at PhosphoSitePlus URLs, kept hitting the license-gated HTML pages, and then just gave up and submitted. Never tried SIGNOR or UniProt. I'm skeptical that those 10 entries are even real since it never actually pulled data from anywhere.

Paper-informed was actually worse, which surprised me. 37 entries but precision dropped to 8% and recall was lower than naive. You'd think handing it the full paper plus the PSP download link would help, but it just tried a few PhosphoSitePlus URLs and HGNC, then bailed after 3 turns. Completely ignored SIGNOR and UniProt even though the paper literally tells it to use them. Feels like the extra context made it more confident about hallucinating rather than more thorough.

Pipeline-guided was the worst. Only 3 entries. The model hallucinated a fake GitHub repo (`phosphoatlas/phosphoatlas-databases`), got a 404, wrote a short apology, and submitted 3 entries before quitting. I think the pipeline prompt confused it — it references tool names like `list_databases` and `query_by_kinase` that belong to the local DatabaseTools API, but this runner only has HTTP, so the model was reaching for tools that weren't there.

Big picture: Mistral Large just quits after 3–4 turns no matter what prompt you give it, and giving it more context seems to make things worse instead of better. Lot of hallucination across all three runs. Need to dig into the actual atlas entries and check how much of it is made up.

2. Qwen3-235B by Andrew: UniProt API worked, but other database API access failed. Log the failures in details and upload the atlas (even failed) if any.

2. Gemini by Neel: 
	- Failures 
		- Gemini hallucinates and goes to localhost urls (from whatever it found searching). this has to be explicity handled.
		- It will need to paginate results as well, as it initially tries to grab large datasets all at once
		- google recently updated its SDK for genai, so there were a ton of compatibility/syntax issues at first
		- There are strict rate limits on the latest pro models
		- pro 2.5 has high server traffic throughout the day
		- using 2.5-flash since its the most stable one
		- will test pro-preview models once rate limit abates
	- Successes
		-
