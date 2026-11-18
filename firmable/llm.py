"""Model calls for the Part 2 agent, with a token and cost ledger.

Every call returns parsed JSON plus a usage record. The agent writes those into
its run state, so cost per step and per source comes from real API usage
numbers, not estimates.
"""

import json
import os
import re
import time

import anthropic
from dotenv import load_dotenv

from . import ROOT

load_dotenv(ROOT / ".env")

MODEL = os.environ.get("LLM_MODEL") or "claude-sonnet-5-5"
# Hard spend cap per process. A call is refused once this much has been spent.
BUDGET_USD = float(os.environ.get("LLM_BUDGET_USD") or 2.0)
# Running total for this process. It lives in memory only, so each new command starts again at 0:
# the cap limits one run, not total spend over time.
_spent = 0.0
# USD per million tokens: (input, output, cache write, cache read).
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}

_client = None


def client():
    # Created on first use, not at import, so importing this module needs no API key.
    # anthropic.Anthropic() reads ANTHROPIC_API_KEY from the environment (.env was loaded above).
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


class LLMError(Exception):
    """A failed call can still have been billed, so it carries the usage for the ledger."""

    def __init__(self, message, usage=None):
        super().__init__(message)
        self.usage = usage


def cost_usd(model, usage):
    # Tokens are billed at four different rates: fresh input, output, writing to the prompt cache,
    # and reading from it. Returns None for a model with no row in PRICES, so an unknown price is
    # never passed off as $0.
    p = PRICES.get(model)
    if not p:
        return None
    return round(
        (usage["input_tokens"] * p[0] + usage["output_tokens"] * p[1]
         + usage["cache_creation_input_tokens"] * p[2] + usage["cache_read_input_tokens"] * p[3]) / 1e6,
        5,
    )


def _extract_json(text):
    # Models often wrap JSON in a ```json fence or put a sentence before it. Take the fenced block
    # if there is one; otherwise take everything from the first "{" to the last "}".
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    body = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(body)


def call_json(step, system, user, effort="medium", max_tokens=16000):
    """One model call that must return a JSON object. Returns (obj, usage).

    If the reply is not valid JSON, the parse error goes back to the model once
    so it can fix its own output. The usage of both calls is summed.
    """
    global _spent
    messages = [{"role": "user", "content": user}]
    total = {"input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "calls": 0}
    t0 = time.time()

    def failed(msg):
        total.update(model=MODEL, seconds=round(time.time() - t0, 1), usd=cost_usd(MODEL, total))
        return LLMError(msg, usage=total)

    # At most 2 attempts: the real call, plus one retry if the reply was not valid JSON.
    for attempt in range(2):
        # The cap is checked before each call. A call that starts just under the cap still runs to
        # completion, so the total can end slightly above it.
        if _spent >= BUDGET_USD:
            raise failed(f"{step}: budget cap reached (${_spent:.2f} of LLM_BUDGET_USD=${BUDGET_USD:.2f})")
        resp = client().messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            # cache_control marks the system prompt for prompt caching. It is long (it contains the
            # whole ontology and the transform list) and identical on every call, so after the first
            # call it is billed at the much cheaper cache-read rate.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            output_config={"effort": effort},
        )
        # The API reports the tokens it actually billed. They are added to the ledger before the
        # reply is even looked at, because a refused, cut-off or unparseable reply still costs money.
        u = resp.usage
        total["calls"] += 1
        call = {k: getattr(u, k, 0) or 0 for k in
                ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")}
        for k, v in call.items():
            total[k] += v
        _spent += cost_usd(MODEL, call) or 0
        if resp.stop_reason == "refusal":
            raise failed(f"{step}: model refused ({getattr(resp.stop_details, 'category', None)})")
        if resp.stop_reason == "max_tokens":
            raise failed(f"{step}: hit max_tokens={max_tokens}")
        text = "".join(b.text for b in resp.content if b.type == "text")
        try:
            obj = _extract_json(text)
            break
        except (ValueError, json.JSONDecodeError) as e:
            if attempt == 1:
                raise failed(f"{step}: reply was not valid JSON twice: {e}")
            # Self-correction: append the model's bad reply and the parser's error to the
            # conversation, then loop round so it can fix its own output.
            messages += [
                {"role": "assistant", "content": resp.content},
                {"role": "user", "content": f"That was not valid JSON ({e}). Reply with only the corrected JSON object."},
            ]
    total["model"] = resp.model
    total["seconds"] = round(time.time() - t0, 1)
    total["usd"] = cost_usd(MODEL, total)
    return obj, total
