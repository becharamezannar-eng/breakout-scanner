"""AI calls: Claude (default) or OpenAI, always with a strict JSON schema, retries and cost tracking.

Plain HTTPS (requests) is used instead of the vendor SDKs to keep the install small and the behaviour explicit.
"""
import json
import logging
import threading
import time

import requests

from .config import PRICES

log = logging.getLogger("intel.llm")


class BudgetExceeded(RuntimeError):
    pass


class LLM:
    def __init__(self, settings):
        self.s = settings
        self.lock = threading.Lock()
        self.calls, self.input_tokens, self.output_tokens, self.cost = 0, 0, 0.0, 0.0

    @property
    def model_name(self):
        return self.s.model if self.s.provider == "anthropic" else self.s.openai_model

    def _account(self, usage_in, usage_out):
        pin, pout = PRICES.get(self.s.model, (3.0, 15.0)) if self.s.provider == "anthropic" else (2.0, 8.0)
        with self.lock:
            self.calls += 1
            self.input_tokens += usage_in
            self.output_tokens += usage_out
            self.cost += usage_in / 1e6 * pin + usage_out / 1e6 * pout

    def json(self, system, user, schema, name="result", max_tokens=16000):
        """Returns the parsed JSON object that matches `schema`."""
        if self.cost > self.s.max_daily_cost:
            raise BudgetExceeded(f"AI spending for this brief passed ${self.s.max_daily_cost:.2f}; stopped")
        last = None
        for attempt in range(4):
            try:
                if self.s.provider == "openai":
                    return self._openai(system, user, schema, name, max_tokens)
                return self._anthropic(system, user, schema, max_tokens)
            except (requests.RequestException, ValueError, KeyError) as e:
                last = e
                wait = 5 * 2 ** attempt
                log.warning("AI call failed (%s), retry in %ss", str(e)[:200], wait)
                time.sleep(wait)
        raise RuntimeError(f"AI call failed after retries: {last}")

    def _anthropic(self, system, user, schema, max_tokens):
        body = dict(model=self.s.model, max_tokens=max_tokens, system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config={"format": {"type": "json_schema", "schema": schema}})
        if self.s.effort:
            body["output_config"]["effort"] = self.s.effort
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=600, json=body,
                          headers={"x-api-key": self.s.anthropic_key, "anthropic-version": "2023-06-01",
                                   "content-type": "application/json"})
        if r.status_code in (429, 500, 502, 503, 529):
            raise requests.RequestException(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"Claude API error {r.status_code}: {r.text[:500]}")      # not retried: config problem
        d = r.json()
        u = d.get("usage", {})
        self._account(u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) +
                      u.get("cache_creation_input_tokens", 0), u.get("output_tokens", 0))
        if d.get("stop_reason") == "max_tokens":
            raise ValueError("answer cut off at max_tokens")
        if d.get("stop_reason") == "refusal":
            raise RuntimeError("the model declined this request")
        text = next(b["text"] for b in d["content"] if b.get("type") == "text")
        return json.loads(text)

    def _openai(self, system, user, schema, name, max_tokens):
        body = dict(model=self.s.openai_model, max_completion_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True,
                                                                            "schema": schema}})
        r = requests.post("https://api.openai.com/v1/chat/completions", timeout=600, json=body,
                          headers={"Authorization": f"Bearer {self.s.openai_key}"})
        if r.status_code in (429, 500, 502, 503):
            raise requests.RequestException(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI API error {r.status_code}: {r.text[:500]}")
        d = r.json()
        u = d.get("usage", {})
        self._account(u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
        return json.loads(d["choices"][0]["message"]["content"])

    def stats(self):
        return dict(model=self.model_name, calls=self.calls, input_tokens=self.input_tokens,
                    output_tokens=self.output_tokens, cost_usd=round(self.cost, 4))
