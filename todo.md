1. Mistral by Borna: 

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
1. Mistral by Borna: log the original url failure, and upload atlas (even failed) if any

2. Qwen3-235B by Andrew: UniProt API worked, but other database API access failed. Log the failures in details and upload the atlas (even failed) if any.
