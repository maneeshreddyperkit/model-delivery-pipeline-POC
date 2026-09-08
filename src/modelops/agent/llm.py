"""Ollama client.

Local by construction: Ollama runs on this machine and serves on
127.0.0.1:11434. Nothing here reaches the internet, which is the whole
reason a local model was chosen over a hosted API.

Written against the standard library only. The `ollama` Python package
is a thin wrapper over the same HTTP endpoints, and one fewer dependency
on a locked-down machine is worth more than the convenience.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_HOST = "http://127.0.0.1:11434"

# Ordered by preference. qwen3 is here because it does native tool calling
# and scores well on it at this size; the 4b is the fallback for a machine
# that cannot hold the 8b. Anything else the user happens to have pulled is
# tried last, because a model without tool support will at least still
# answer questions.
PREFERRED_MODELS = ["qwen3:8b", "qwen3:4b", "llama3.1:8b", "qwen2.5:7b"]


@dataclass
class Health:
    available: bool
    model: str | None = None
    installed: list[str] | None = None
    detail: str = ""


class Ollama:
    def __init__(self, host: str = DEFAULT_HOST, model: str | None = None,
                 timeout: float = 120.0):
        self.host = host.rstrip("/")
        self.model = model
        self.timeout = timeout

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def health(self, timeout: float = 0.6) -> Health:
        """Is Ollama up, and which model should we use?

        Short timeout on purpose. Ollama is on the loopback interface, so
        if it is running it answers in single-digit milliseconds; anything
        longer means it is not there. The chat panel probes this on open
        and a dead Ollama must not make the app feel slow.
        """
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=timeout) as r:
                tags = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return Health(False, detail=f"Ollama is not reachable at {self.host} ({exc}).")

        installed = [m["name"] for m in tags.get("models", [])]
        if not installed:
            return Health(False, installed=[],
                          detail="Ollama is running but no models are pulled. "
                                 "Try: ollama pull qwen3:8b")

        if self.model and self.model in installed:
            return Health(True, self.model, installed)
        for candidate in PREFERRED_MODELS:
            # Ollama reports names with an explicit tag, so match both forms.
            for name in installed:
                if name == candidate or name.split(":")[0] == candidate.split(":")[0]:
                    return Health(True, name, installed)
        return Health(True, installed[0], installed,
                      detail=f"Using {installed[0]}; none of the preferred "
                             f"tool-calling models are installed.")

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             *, think: bool = False) -> dict:
        """One turn. Returns the raw message dict from Ollama.

        `think` is off by default. qwen3 emits a reasoning block when it is
        on, which roughly triples latency for no gain here: the tool schemas
        already carry the structure the model would otherwise reason its way
        towards, and a demo cannot afford eighty seconds of silence.
        """
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": think,
            "options": {"temperature": 0.1, "num_ctx": 8192},
        }
        if tools:
            payload["tools"] = tools
        return self._post("/api/chat", payload).get("message", {})

    def warm(self) -> bool:
        """Load the model into memory without asking it anything.

        Worth doing before a demo. The first request to a cold model pays
        the weight-loading cost, which on an 8b model is long enough that
        it reads as a hang.
        """
        try:
            self._post("/api/chat",
                       {"model": self.model, "messages": [], "stream": False},
                       timeout=300.0)
            return True
        except (urllib.error.URLError, OSError):
            return False
