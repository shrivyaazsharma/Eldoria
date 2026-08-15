"""
Domain A - shared LLM client.
 
Every generator in the project goes through this file. It handles the three
things that will otherwise bite you in every single generator separately:
rate limits, malformed JSON, and provider outages.
 
Write this once, together, then leave it alone.
"""
 
import os
import re
import json
import time
import random
from typing import Optional
 
from dotenv import load_dotenv
 
load_dotenv()
 
GEMINI_MODEL = "gemini-2.5-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"
 
MAX_RETRIES = 5
BASE_DELAY = 2.0
 
 
class LLMError(RuntimeError):
    """Raised when every provider and every retry has been exhausted."""
 
 
# --------------------------------------------------------------------------
# providers
# --------------------------------------------------------------------------
 
_gemini_client = None
_groq_client = None
 
 
def _gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
 
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise LLMError("GEMINI_API_KEY missing. Copy .env.example to .env.")
        _gemini_client = genai.Client(api_key=key)
    return _gemini_client
 
 
def _groq():
    global _groq_client
    if _groq_client is None:
        key = os.getenv("GROQ_API_KEY")
        if not key:
            return None
        from groq import Groq
 
        _groq_client = Groq(api_key=key)
    return _groq_client
 
 
def _is_rate_limited(err: Exception) -> bool:
    text = str(err).upper()
    return "429" in text or "RESOURCE_EXHAUSTED" in text or "RATE" in text
 
 
def _call_gemini(prompt: str, system: str, temperature: float, json_mode: bool) -> str:
    config = {"system_instruction": system, "temperature": temperature}
    if json_mode:
        config["response_mime_type"] = "application/json"
    resp = _gemini().models.generate_content(
        model=GEMINI_MODEL, contents=prompt, config=config
    )
    return resp.text
 
 
def _call_groq(prompt: str, system: str, temperature: float, json_mode: bool) -> str:
    client = _groq()
    if client is None:
        raise LLMError("No GROQ_API_KEY set, cannot fall back.")
    kwargs = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = client.chat.completions.create(
        model=GROQ_MODEL,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        **kwargs,
    )
    return resp.choices[0].message.content
 
 
# --------------------------------------------------------------------------
# json repair
# --------------------------------------------------------------------------
 
def parse_json(raw: str) -> dict:
    """
    Models wrap JSON in markdown fences, add a preamble, or trail a comma.
    Try progressively more aggressive recovery before giving up.
    """
    if raw is None:
        raise LLMError("Model returned no text at all.")
 
    attempts = [raw, raw.strip()]
 
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        attempts.append(fenced.group(1))
 
    # grab the outermost {...} or [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = raw.find(opener), raw.rfind(closer)
        if start != -1 and end > start:
            attempts.append(raw[start : end + 1])
 
    for candidate in attempts:
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            # one last go: strip trailing commas
            try:
                return json.loads(re.sub(r",(\s*[}\]])", r"\1", candidate))
            except Exception:
                continue
 
    raise LLMError(f"Could not parse JSON. First 400 chars:\n{raw[:400]}")
 
 
# --------------------------------------------------------------------------
# public api
# --------------------------------------------------------------------------
 
def generate_text(
    prompt: str,
    system: str = "",
    temperature: float = 1.0,
    allow_fallback: bool = True,
) -> str:
    """Raw text generation with exponential backoff and a Groq fallback."""
    last_err: Optional[Exception] = None
 
    for attempt in range(MAX_RETRIES):
        try:
            return _call_gemini(prompt, system, temperature, json_mode=False)
        except Exception as err:  # noqa: BLE001
            last_err = err
            if not _is_rate_limited(err):
                break
            delay = BASE_DELAY * (2**attempt) + random.uniform(0, 1)
            print(f"  [llm] rate limited, retrying in {delay:.1f}s "
                  f"({attempt + 1}/{MAX_RETRIES})")
            time.sleep(delay)
 
    if allow_fallback and _groq() is not None:
        print("  [llm] falling back to Groq")
        try:
            return _call_groq(prompt, system, temperature, json_mode=False)
        except Exception as err:  # noqa: BLE001
            last_err = err
 
    raise LLMError(f"All providers failed. Last error: {last_err}")
 
 
def generate_json(
    prompt: str,
    system: str = "",
    temperature: float = 1.0,
    allow_fallback: bool = True,
) -> dict:
    """Same as generate_text but forces JSON mode and parses the result."""
    last_err: Optional[Exception] = None
 
    for attempt in range(MAX_RETRIES):
        try:
            return parse_json(_call_gemini(prompt, system, temperature, json_mode=True))
        except Exception as err:  # noqa: BLE001
            last_err = err
            if not _is_rate_limited(err):
                break
            delay = BASE_DELAY * (2**attempt) + random.uniform(0, 1)
            print(f"  [llm] rate limited, retrying in {delay:.1f}s "
                  f"({attempt + 1}/{MAX_RETRIES})")
            time.sleep(delay)
 
    if allow_fallback and _groq() is not None:
        print("  [llm] falling back to Groq")
        try:
            system_json = system + "\n\nRespond with valid JSON only."
            return parse_json(_call_groq(prompt, system_json, temperature, json_mode=True))
        except Exception as err:  # noqa: BLE001
            last_err = err
 
    raise LLMError(f"All providers failed. Last error: {last_err}")
 
 
if __name__ == "__main__":
    out = generate_json(
        "Name three fictional taverns and one rumour heard in each.",
        system='Return JSON: {"taverns":[{"name":str,"rumour":str}]}',
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))
 