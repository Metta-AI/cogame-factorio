"""Reference LLM policy: ask Claude for each step's program.

Every step the policy prompts a Claude model with the FLE API reference
(``welcome.api_docs``), the task, the current observation (``raw_text``,
inventory, score) and a rolling window of the last few programs and their
outputs, and asks for one Python program in a fenced code block. The
fenced block is the reply; on any API failure the reply is ``pass``.

Providers (chosen by ``COGAME_LLM_PROVIDER``, else auto-detected):

- ``anthropic`` — the Claude API via the ``anthropic`` SDK, credentials
  from ``ANTHROPIC_API_KEY`` (or an ``ant auth login`` profile).
- ``bedrock`` — Claude on Amazon Bedrock via the same SDK's Bedrock client
  (needs ``boto3``; AWS credentials/region from the environment).
- ``none`` — no LLM: behaves like the idle player.

The dependencies are optional and imported lazily; the policy image does
not ship them unless installed: ``uv sync --extra llm`` (or
``pip install "cogame-factorio[llm]"``). Model: ``COGAME_LLM_MODEL``
(default ``claude-haiku-4-5-20251001``; on Bedrock
``us.anthropic.claude-haiku-4-5-20251001-v1:0``).

``python -m players.llm_player``
"""

from __future__ import annotations

import os
import re
import sys
from collections import deque

from players import fle_helpers as H
from players.client import Policy, main_for

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Bedrock inference profiles, tried in order: model access is a per-account
# Marketplace subscription and hosted capacity is shared account-wide, so a
# 403/429 on one id falls through to the next. ``BEDROCK_MODEL`` (set by
# `coworld upload-policy --use-bedrock --bedrock-model ...`) or
# ``COGAME_LLM_MODEL`` pins a single id ahead of these.
#
# Deliberately one entry. A tournament round is priced against the coworld's
# daily envelope (spec 0080), and a mixed list means a Haiku 403/429 silently
# escalates the league to Sonnet/Opus pricing with nothing in the league UI to
# say so. Bounding cost costs availability instead: a Haiku outage idles the
# policy (it replies ``pass``, scoring near the idle floor) rather than quietly
# spending more. Re-add a fallback here only alongside a raised envelope.
BEDROCK_MODEL_CANDIDATES = [
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
]
DEFAULT_BEDROCK_MODEL = BEDROCK_MODEL_CANDIDATES[0]
NOOP = "pass"

# Keep the prompt bounded: FLE observations can be huge.
MAX_RAW_TEXT_CHARS = 12_000
MAX_OUTPUT_CHARS = 4_000
MAX_API_DOCS_CHARS = 120_000
HISTORY_STEPS = 4

_FENCE_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)

SYSTEM_PREAMBLE = """You are playing the Factorio Learning Environment through cogame-factorio.
Each turn you receive the current observation and must reply with exactly one
Python program in a single ```python fenced block. The program runs against a
persistent namespace (variables from earlier programs are still defined) using
the FLE agent API documented below. Programs that raise are not fatal: the
traceback comes back next turn, so prefer many small robust steps and wrap
risky calls in try/except and print() what happened. Do not use names starting
with an underscore for variables you want to keep. Never write an infinite
loop; a program has a wall-clock timeout. Aim to raise the production score
(open play) or the target item throughput (throughput tasks) as fast as
possible: build burner drills onto stone furnaces, keep everything fuelled,
and let the factory run with sleep(...) at the end of each program.
"""


def _provider_from_env() -> str:
    explicit = os.environ.get("COGAME_LLM_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    # `coworld upload-policy --use-bedrock` sets USE_BEDROCK=true (+ BEDROCK_MODEL);
    # hosted pods reach Bedrock through a sidecar (endpoint + bearer token).
    if os.environ.get("USE_BEDROCK", "").strip().lower() in ("1", "true", "yes") \
            or os.environ.get("AWS_ENDPOINT_URL_BEDROCK_RUNTIME") \
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        return "bedrock"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE") \
            or os.environ.get("AWS_ROLE_ARN") or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE"):
        return "bedrock"
    return "none"


def extract_program(text: str) -> str | None:
    """The first fenced Python block in ``text`` (or the whole text if it
    looks like bare code); None if nothing usable."""
    if not isinstance(text, str) or not text.strip():
        return None
    blocks = _FENCE_RE.findall(text)
    if blocks:
        code = max(blocks, key=len).strip("\n")
        return code or None
    if "```" not in text:
        return text.strip("\n") or None
    return None


class _BedrockHttpClient:
    """Minimal InvokeModel client over the Bedrock runtime endpoint (or the
    hosted sidecar named by AWS_ENDPOINT_URL_BEDROCK_RUNTIME) authenticating with
    AWS_BEARER_TOKEN_BEDROCK. Exposes the ``messages.create`` shape the policy
    uses so both transports share one call site."""

    class _Block:
        def __init__(self, d):
            self.type = d.get("type", "")
            self.text = d.get("text", "")

    class _Response:
        def __init__(self, d):
            self.stop_reason = d.get("stop_reason")
            self.content = [_BedrockHttpClient._Block(b) for b in d.get("content", [])]

    def __init__(self, timeout: float):
        import urllib.request  # noqa: PLC0415
        self._urllib = urllib.request
        region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"
        endpoint = (os.environ.get("AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "").strip()
                    or f"https://bedrock-runtime.{region}.amazonaws.com")
        self.endpoint = endpoint.rstrip("/")
        self.token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK", "").strip()
        self.timeout = timeout
        self.messages = self  # so `client.messages.create(...)` works

    def with_options(self, **_kwargs):
        return self

    def create(self, *, model: str, max_tokens: int, system, messages):
        import json  # noqa: PLC0415
        body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": max_tokens,
                "system": system, "messages": messages}
        req = self._urllib.Request(
            f"{self.endpoint}/model/{model}/invoke",
            data=json.dumps(body).encode(), method="POST",
            headers={"content-type": "application/json", "accept": "application/json",
                     **({"authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with self._urllib.urlopen(req, timeout=self.timeout) as resp:
                return self._Response(json.loads(resp.read().decode()))
        except Exception as exc:  # noqa: BLE001
            detail = ""
            if hasattr(exc, "read"):
                try:
                    detail = exc.read().decode(errors="replace")[:300]  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001
                    detail = ""
            raise RuntimeError(f"bedrock invoke {model}: {exc!r} {detail}") from exc


class LLMPolicy(Policy):
    """One Claude call per step; ``pass`` whenever the model is unavailable."""

    def __init__(self, provider: str | None = None, model: str | None = None,
                 timeout_seconds: float | None = None):
        self.provider = (provider or _provider_from_env()).lower()
        pinned = model or os.environ.get("COGAME_LLM_MODEL") or (
            os.environ.get("BEDROCK_MODEL") if self.provider == "bedrock" else None)
        if self.provider == "bedrock":
            # Pinned id first, then the shared candidate list as fallbacks (an
            # unsubscribed/exhausted profile must not idle the whole episode).
            self._models: list[str] = [m for m in ([pinned] if pinned else []) + BEDROCK_MODEL_CANDIDATES
                                       if m] 
            self._models = list(dict.fromkeys(self._models))
        else:
            self._models = [pinned or DEFAULT_MODEL]
        self.model = self._models[0]
        self.timeout = timeout_seconds or float(os.environ.get("COGAME_LLM_TIMEOUT", "40"))
        self.api_docs = ""
        self.task = {}
        self.history: deque[tuple[int, str, str]] = deque(maxlen=HISTORY_STEPS)
        self._client = None
        self._cache_ok = True
        self._disabled = self.provider == "none"
        if self._disabled:
            self._log("no LLM provider configured; replying 'pass' every step")

    @staticmethod
    def _log(msg: str) -> None:
        print(f"llm_player: {msg}", file=sys.stderr, flush=True)

    # -- client construction (lazy, optional deps) --------------------------

    def _client_or_none(self):
        if self._client is not None or self._disabled:
            return self._client
        try:
            import anthropic  # noqa: PLC0415 - optional dependency
        except Exception as exc:  # noqa: BLE001
            self._log(f"anthropic SDK unavailable ({exc!r}); install with "
                      f"`uv sync --extra llm`. Replying 'pass'.")
            self._disabled = True
            return None
        try:
            if self.provider == "bedrock" and (os.environ.get("AWS_ENDPOINT_URL_BEDROCK_RUNTIME")
                                               or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")):
                # Hosted pods: a Bedrock sidecar/bearer token (the SDK's IAM
                # signing path 403s with "Invalid API Key format" there).
                self._client = _BedrockHttpClient(timeout=self.timeout)
                return self._client
            if self.provider == "bedrock":
                region = (os.environ.get("AWS_REGION")
                          or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
                # The classic InvokeModel client accepts Bedrock inference-profile
                # ids (us.anthropic.claude-...); the Mantle Messages endpoint
                # 404s those ids in the accounts we tested (2026-08-18).
                client = anthropic.AnthropicBedrock(aws_region=region)
            else:
                client = anthropic.Anthropic()
            self._client = client.with_options(timeout=self.timeout, max_retries=1)
        except Exception as exc:  # noqa: BLE001
            self._log(f"could not build {self.provider} client ({exc!r}); replying 'pass'")
            self._disabled = True
            return None
        return self._client

    # -- Policy hooks -------------------------------------------------------

    def on_welcome(self, welcome: dict) -> None:
        docs = welcome.get("api_docs")
        if isinstance(docs, str):
            self.api_docs = docs[:MAX_API_DOCS_CHARS]
        task = welcome.get("task")
        self.task = task if isinstance(task, dict) else {}
        episode = welcome.get("episode")
        if isinstance(episode, dict):
            self.task = {**self.task, "episode": {
                k: episode.get(k) for k in (
                    "max_steps", "step_deadline_seconds",
                    "program_timeout_seconds", "starting_inventory")}}

    def _system_prompt(self) -> str:
        parts = [SYSTEM_PREAMBLE]
        if self.task:
            parts.append("Task:\n" + _compact(self.task, 4000))
        if self.api_docs:
            parts.append("FLE API reference:\n" + self.api_docs)
        return "\n\n".join(parts)

    def _user_prompt(self, step: int, observation: dict) -> str:
        last = H.last_program(observation)
        if isinstance(last.get("code"), str):
            self.history.append((step - 1, last["code"],
                                 H.last_output(observation)[:MAX_OUTPUT_CHARS]))
        lines = [f"Step {step}. Score so far: {H.score(observation):.1f}."]
        inv = H.inventory(observation)
        if inv:
            lines.append("Inventory: " + ", ".join(
                f"{k}={v}" for k, v in sorted(inv.items()) if v))
        raw = H.raw_text(observation)
        if raw:
            lines.append("Observation:\n" + raw[:MAX_RAW_TEXT_CHARS])
        if self.history:
            lines.append("Recent programs and their outputs:")
            for s, code, out in self.history:
                lines.append(f"--- step {s} program ---\n{code}\n--- output ---\n{out}")
        lines.append("Reply with the next program in one ```python block.")
        return "\n\n".join(lines)

    def program(self, step: int, observation: dict) -> str:
        user = self._user_prompt(step, observation)
        client = self._client_or_none()
        if client is None:
            return NOOP
        try:
            system = [{"type": "text", "text": self._system_prompt()}]
            if self._cache_ok:
                system[0]["cache_control"] = {"type": "ephemeral"}
            try:
                response = client.messages.create(
                    model=self.model, max_tokens=4096, system=system,
                    messages=[{"role": "user", "content": user}],
                )
            except Exception as exc:  # noqa: BLE001
                if self._cache_ok and "cache_control" in str(exc):
                    self._cache_ok = False  # provider/model without prompt caching
                    self._log("prompt caching rejected; retrying without it")
                    response = client.messages.create(
                        model=self.model, max_tokens=4096, system=system[0]["text"],
                        messages=[{"role": "user", "content": user}],
                    )
                else:
                    raise
        except Exception as exc:  # noqa: BLE001 - any API failure -> pass
            self._log(f"API call failed at step {step} on {self.model}: {exc!r}")
            # Fall through the candidate list on access/capacity errors so one
            # unsubscribed or exhausted profile does not idle the whole episode.
            idx = self._models.index(self.model) if self.model in self._models else 0
            if idx + 1 < len(self._models):
                self.model = self._models[idx + 1]
                self._log(f"switching to {self.model} for the next step")
            return NOOP
        if getattr(response, "stop_reason", None) == "refusal":
            self._log(f"model refused at step {step}; replying 'pass'")
            return NOOP
        text = "".join(getattr(b, "text", "") for b in getattr(response, "content", [])
                       if getattr(b, "type", "") == "text")
        code = extract_program(text)
        if code is None:
            self._log(f"no program in the reply at step {step}; replying 'pass'")
            return NOOP
        return code


def _compact(obj, limit: int) -> str:
    import json
    s = json.dumps(obj, default=str)
    return s if len(s) <= limit else s[:limit] + "...(truncated)"


if __name__ == "__main__":
    main_for(LLMPolicy)
