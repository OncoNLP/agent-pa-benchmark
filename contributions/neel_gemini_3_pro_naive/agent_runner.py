from google import genai
from google.genai import types
import json
import time
from typing import List, Tuple, Dict

class GeminiAgent(BaseAgent):
    def __init__(self, api_key: str, model_name: str = "gemini-3-flash", **kwargs):
        super().__init__(model_name=model_name, **kwargs)
        
        # 1. Configure the Retry Strategy
        # This will automatically retry on 429 (Rate Limit) and 5xx (Server Error)
        self.retry_config = types.HttpRetryOptions(
            attempts=10,             # Max number of retries
            initial_delay=2.0,       # Start with 2 seconds
            max_delay=60.0,          # Don't wait longer than 1 minute between tries
            exp_base=2.0,            # Double the wait time each failure
            jitter=0.2,              # Add 20% randomness to prevent "thundering herd"
            http_status_codes=[429, 500, 502, 503, 504]
        )

        # 2. Initialize client with these options
        self.client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                retry_options=self.retry_config,
                timeout=300000 # 5-minute timeout for large curation responses
            )
        )

    def _call_model(self, messages: List[Dict], tools: List[Dict]) -> types.GenerateContentResponse:
        """
        Calls Gemini 3. The SDK will now handle 429 errors internally 
        before this method even returns.
        """
        contents = []
        system_instruction = None

        for msg in messages:
            if msg["role"] == "system":
                system_instruction = types.Content(parts=[types.Part(text=msg["content"])])
            else:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append(types.Content(role=role, parts=[types.Part(text=msg["content"])]))

        # We pass thinking_config to allow the agent to plan database lookups
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            thinking_config=types.ThinkingConfig(thinking_level="MEDIUM")
        )

        # This call is now protected by our HttpRetryOptions
        return self.client.models.generate_content(
            model=self.model_name,
            contents=contents,
            config=config
        )

    def _parse_tool_calls(self, response: types.GenerateContentResponse) -> List[Tuple[str, Dict]]:
        tool_calls = []
        if not response.candidates: return []

        for part in response.candidates[0].content.parts:
            if part.function_call:
                tool_calls.append((part.function_call.name, part.function_call.args))
        return tool_calls

    def _parse_text(self, response: types.GenerateContentResponse) -> str:
        return response.text or ""

    def _format_tool_result(self, tool_name: str, result: Dict) -> Dict:
        # Crucial for agents: feed the JSON result back so it can be parsed
        return {
            "role": "user",
            "content": f"DATABASE_RESULT [{tool_name}]: {json.dumps(result)}"
        }
    
naive_prompt = """
                    You are a bioinformatics researcher tasked with building a comprehensive human protein phosphorylation atlas from available databases.

                    Your goal: Curate ALL known human kinase-substrate-phosphosite relationships by systematically querying the databases available to you.

                    For each relationship, you must capture:
                    - Kinase gene symbol (the enzyme)
                    - Substrate gene symbol (the target protein)
                    - Phosphorylation site (e.g., Y15, S10, T161)
                    - Heptameric peptide sequence around the site (if available)
                    - Substrate UniProt accession (if available)
                    - Which database(s) support this relationship

                    Requirements:
                    1. Be EXHAUSTIVE — the atlas should contain every kinase-substrate-site triplet present in the databases. Missing entries is worse than having extra entries.
                    2. Cross-reference across databases — if the same relationship appears in multiple databases, record all supporting sources.
                    3. Do NOT fabricate data. Only include relationships returned by the tools.

                    Start by discovering what databases are available, then develop and execute a systematic curation strategy.

                    When you are finished, call the "submit_atlas" tool with your complete results.
                """