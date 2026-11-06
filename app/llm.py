"""A small client of an OpenAI-compatible chat endpoint (`/v1/chat/completions`).

The endpoint is the user's choice: `OPENMONTAGE_LLM_BASE_URL` (for example
`http://192.168.200.1:11434/v1` for Ollama on the node, or any OpenAI-compatible server),
`OPENMONTAGE_LLM_MODEL` and `OPENMONTAGE_LLM_API_KEY` (sensitive; optional for a local
server). With no base URL the service has no LLM and plans videos without one
(`director.plan_without_llm`).

The service asks for JSON only (`response_format` json_object, which Ollama, vLLM,
llama.cpp server and OpenAI accept) and checks it; an answer that is not valid JSON gets
one repair turn.
"""

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("llm")


class LLMError(Exception):
    pass


class LLM:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.base_url = (env.get("OPENMONTAGE_LLM_BASE_URL") or "").strip().rstrip("/")
        self.model = (env.get("OPENMONTAGE_LLM_MODEL") or "").strip() or "qwen2.5:14b-instruct"
        self._key = (env.get("OPENMONTAGE_LLM_API_KEY") or "").strip()
        self.timeout = float(env.get("OPENMONTAGE_LLM_TIMEOUT_S") or 300)
        self.temperature = float(env.get("OPENMONTAGE_LLM_TEMPERATURE") or 0.4)
        self.calls = 0
        self.seconds = 0.0

    @property
    def configured(self):
        return bool(self.base_url)

    def describe(self):
        return "%s at %s" % (self.model, self.base_url) if self.configured else "none"

    def _url(self):
        # Accept a base with or without /v1.
        return self.base_url + ("/chat/completions" if self.base_url.endswith("/v1") else "/v1/chat/completions")

    def chat(self, messages, json_mode=True, max_tokens=1500):
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = "Bearer " + self._key
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": max_tokens, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        start = time.monotonic()
        try:
            response = requests.post(self._url(), json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as error:
            raise LLMError("the LLM endpoint cannot be reached (%s)" % type(error).__name__) from None
        finally:
            self.calls += 1
            self.seconds += time.monotonic() - start
        if response.status_code in (401, 403):
            raise LLMError("the LLM endpoint refused the key (HTTP %d): check OPENMONTAGE_LLM_API_KEY"
                           % response.status_code)
        if response.status_code != 200:
            raise LLMError("the LLM endpoint answered HTTP %d" % response.status_code)
        try:
            return response.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("the LLM endpoint gave an answer that is not a chat completion") from None

    def json(self, messages, max_tokens=1500):
        """The JSON object of the answer, with one repair turn."""
        text = self.chat(messages, max_tokens=max_tokens)
        try:
            return parse_json(text)
        except ValueError as error:
            log.info("LLM answer is not JSON (%s): one repair turn", error)
            repair = messages + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": "That was not one valid JSON object. Answer again with "
                                            "only the JSON object, no other text."}]
            return parse_json(self.chat(repair, max_tokens=max_tokens))


def parse_json(text):
    """The first JSON object in `text` (a model can wrap it in a code fence)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("not a JSON object")
    return value
