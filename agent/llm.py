"""Provider-neutral LLM client.

Everything here speaks the OpenAI chat-completions dialect, so the same code works
against OmniRoute (self-hosted on TrueNAS), OpenRouter, a bare Ollama/vLLM endpoint, or
OpenAI itself. Only `LLM_BASE_URL` changes.

Three provider behaviours this module defends against, because each one fails *silently*
and would otherwise look like a model quality problem rather than a plumbing problem:

1. **Tools silently dropped.** OmniRoute classifies upstream providers as `native`,
   `emulated` (regex-parsed `<tool>{...}</tool>` blocks) or `none` — and `none` drops the
   `tools` array without error. An agent pointed at such a provider appears to work and
   simply never calls a tool. `preflight()` proves tool calling actually happens before
   the service accepts traffic.

2. **Structured outputs not honoured.** `response_format: {type: "json_schema"}` is not
   universally supported. `structured()` degrades through three strategies and reports
   which one worked, rather than assuming the first one did.

3. **Prompt rewriting / token compression.** OmniRoute can compress "tool output,
   repeated context and bloated JSON" to save 15-95% of tokens. A routine plan and a
   candidate exercise list are precisely that shape, and a rewritten exercise id is a
   corrupted routine. Compression is therefore disabled by default on this client via the
   `x-omniroute-compression` header; opt back in per call if you want it for chat.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from openai import OpenAI
from openai import APIStatusError, BadRequestError

log = logging.getLogger(__name__)

# No useful default: the gateway address depends on the deployment, and a wrong
# default that silently fails is worse than an obvious one.
DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1")
DEFAULT_API_KEY = os.environ.get("LLM_API_KEY", "not-needed-for-local-gateway")

# Sent on every request. OmniRoute reads this header; other gateways ignore an
# unknown header, so it is safe to always include.
NO_COMPRESSION_HEADERS = {"x-omniroute-compression": "off"}


class ToolsUnsupportedError(RuntimeError):
    """The configured model does not actually perform native tool calling."""


class StructuredOutputError(RuntimeError):
    """The model could not be made to return JSON matching the schema."""


@dataclass
class Capabilities:
    model: str
    base_url: str
    reachable: bool = False
    native_tool_calling: bool = False
    json_schema_response_format: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def usable_for_routines(self) -> bool:
        """Routine generation needs tool calling. Structured output is preferred but
        recoverable, because the programming validator catches a malformed plan."""
        return self.reachable and self.native_tool_calling

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "base_url": self.base_url,
            "reachable": self.reachable,
            "native_tool_calling": self.native_tool_calling,
            "json_schema_response_format": self.json_schema_response_format,
            "usable_for_routines": self.usable_for_routines,
            "notes": self.notes,
        }


class LLMClient:
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        compression: bool = False,
        timeout: float = 300.0,
    ) -> None:
        self.model = model
        self.base_url = base_url or DEFAULT_BASE_URL
        self.compression = compression
        self._client = OpenAI(
            base_url=self.base_url,
            api_key=api_key or DEFAULT_API_KEY,
            timeout=timeout,
            max_retries=2,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if not self.compression:
            headers.update(NO_COMPRESSION_HEADERS)
        if extra:
            headers.update(extra)
        return headers

    def _create(self, **kwargs) -> Any:
        return self._client.chat.completions.create(
            model=kwargs.pop("model", self.model),
            extra_headers=self._headers(kwargs.pop("extra_headers", None)),
            **kwargs,
        )

    # ------------------------------------------------------------------
    # preflight
    # ------------------------------------------------------------------

    def preflight(self) -> Capabilities:
        """Probe what the configured model+gateway actually does.

        Two cheap live calls. Worth it at startup: the alternative is discovering that
        tools are being dropped after a user has waited for a routine that silently
        never searched the exercise database.
        """
        caps = Capabilities(model=self.model, base_url=self.base_url)

        probe_tool = {
            "type": "function",
            "function": {
                "name": "report_ready",
                "description": "Report that the tool channel is working.",
                "parameters": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        }

        try:
            response = self._create(
                messages=[{
                    "role": "user",
                    "content": "Call the report_ready tool with ok=true. Do not reply in text.",
                }],
                tools=[probe_tool],
                tool_choice={"type": "function", "function": {"name": "report_ready"}},
                max_tokens=200,
            )
            caps.reachable = True
            message = response.choices[0].message
            if getattr(message, "tool_calls", None):
                caps.native_tool_calling = True
            else:
                # Either emulated (tool intent left in the text) or dropped entirely.
                text = (message.content or "")[:200]
                if "<tool" in text or "report_ready" in text:
                    caps.notes.append(
                        "Tool call appeared in message text rather than as a structured "
                        "tool_call — this provider emulates tool calling. Prefer a "
                        "provider listed as 'native'; emulated parsing is brittle for "
                        "the nested routine payload."
                    )
                else:
                    caps.notes.append(
                        "Forced tool_choice produced no tool_call at all — this provider "
                        "is silently dropping the tools array. Routine generation cannot "
                        "work on it. Switch to a provider with native tool calling."
                    )
        except APIStatusError as exc:
            caps.notes.append(f"Gateway unreachable or rejected the probe: {exc}")
            return caps
        except Exception as exc:  # noqa: BLE001
            caps.notes.append(f"Probe failed: {type(exc).__name__}: {exc}")
            return caps

        # Structured outputs are a nice-to-have rather than a hard requirement, so a
        # failure here is a note, not a blocker.
        try:
            self._create(
                messages=[{"role": "user", "content": "Return {\"ok\": true}."}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "probe",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                },
                max_tokens=200,
            )
            caps.json_schema_response_format = True
        except (BadRequestError, APIStatusError) as exc:
            caps.notes.append(
                "json_schema response_format rejected; falling back to "
                f"forced-tool-call for structured output. ({type(exc).__name__})"
            )
        except Exception as exc:  # noqa: BLE001
            caps.notes.append(f"Structured-output probe inconclusive: {exc}")

        return caps

    # ------------------------------------------------------------------
    # structured output
    # ------------------------------------------------------------------

    def structured(
        self,
        messages: list[dict],
        schema: dict,
        schema_name: str = "result",
        max_repair_attempts: int = 2,
    ) -> tuple[dict, str]:
        """Return (parsed_object, strategy_used).

        Strategies, in order of reliability:
          json_schema        native structured outputs
          forced_tool_call   a single tool whose parameters ARE the schema
          json_repair        plain generation, then parse, then ask it to fix the parse

        Only JSON *parseability* and shape are handled here. Whether the routine is any
        good is the programming validator's job — keeping those separate is what makes a
        weaker JSON guarantee survivable.
        """
        # 1. Native structured outputs.
        try:
            response = self._create(
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                max_tokens=16000,
            )
            content = response.choices[0].message.content or ""
            return json.loads(content), "json_schema"
        except (BadRequestError, APIStatusError) as exc:
            log.info("json_schema unsupported (%s); trying forced tool call", type(exc).__name__)
        except json.JSONDecodeError:
            log.warning("json_schema returned unparseable JSON; trying forced tool call")

        # 2. Forced tool call. Widely supported where structured outputs are not, since
        #    tool parameters are themselves a JSON Schema the provider must satisfy.
        try:
            response = self._create(
                messages=messages,
                tools=[{
                    "type": "function",
                    "function": {
                        "name": schema_name,
                        "description": f"Return the {schema_name} payload.",
                        "parameters": schema,
                    },
                }],
                tool_choice={"type": "function", "function": {"name": schema_name}},
                max_tokens=16000,
            )
            calls = response.choices[0].message.tool_calls or []
            if calls:
                return json.loads(calls[0].function.arguments), "forced_tool_call"
            log.warning("forced tool call produced no tool_calls; trying json_repair")
        except (BadRequestError, APIStatusError) as exc:
            log.info("forced tool call failed (%s); trying json_repair", type(exc).__name__)
        except json.JSONDecodeError:
            log.warning("tool arguments were unparseable JSON; trying json_repair")

        # 3. Ask plainly, then repair parse failures by showing the model its own error.
        conversation = list(messages) + [{
            "role": "system",
            "content": (
                "Respond with a single JSON object conforming to this schema and nothing "
                "else — no prose, no markdown fences:\n"
                + json.dumps(schema)
            ),
        }]
        last_error = ""
        for attempt in range(max_repair_attempts + 1):
            response = self._create(messages=conversation, max_tokens=16000)
            content = (response.choices[0].message.content or "").strip()
            # Providers commonly wrap JSON in fences despite instructions.
            if content.startswith("```"):
                content = content.split("```")[1] if "```" in content[3:] else content[3:]
                content = content.removeprefix("json").strip()
            try:
                return json.loads(content), "json_repair"
            except json.JSONDecodeError as exc:
                last_error = str(exc)
                if attempt == max_repair_attempts:
                    break
                conversation += [
                    {"role": "assistant", "content": content[:4000]},
                    {"role": "user", "content": (
                        f"That did not parse as JSON: {last_error}. Return only the "
                        "corrected JSON object."
                    )},
                ]

        raise StructuredOutputError(
            f"could not obtain schema-conforming JSON from {self.model} after "
            f"{max_repair_attempts + 1} attempts; last parse error: {last_error}"
        )

    # ------------------------------------------------------------------
    # tool loop
    # ------------------------------------------------------------------

    def run_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], Any],
        max_rounds: int = 12,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> tuple[list[dict], str]:
        """Drive the tool-calling loop. Returns (full message list, final text).

        `dispatch(name, arguments)` executes a tool and returns a JSON-serializable
        result. A tool that raises is reported back to the model as an error result
        rather than crashing the request, so the model can recover or explain.
        """
        conversation = list(messages)

        for round_index in range(max_rounds):
            response = self._create(messages=conversation, tools=tools, max_tokens=16000)
            message = response.choices[0].message
            calls = getattr(message, "tool_calls", None) or []

            # Echo the assistant turn back verbatim, including tool_calls — dropping
            # them breaks the tool_call_id pairing the next turn requires.
            assistant_turn: dict[str, Any] = {
                "role": "assistant",
                "content": message.content,
            }
            if calls:
                assistant_turn["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.function.name,
                            "arguments": c.function.arguments,
                        },
                    }
                    for c in calls
                ]
            conversation.append(assistant_turn)

            if not calls:
                return conversation, message.content or ""

            for call in calls:
                name = call.function.name
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError as exc:
                    result: Any = {"error": f"arguments were not valid JSON: {exc}"}
                else:
                    if on_event:
                        on_event("tool_call", {"name": name, "arguments": arguments})
                    try:
                        result = dispatch(name, arguments)
                    except Exception as exc:  # noqa: BLE001
                        # Surface the failure to the model instead of 500-ing the request.
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                        log.exception("tool %s failed", name)
                    if on_event:
                        on_event("tool_result", {"name": name, "result": result})

                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(result, default=str),
                })

        # Out of rounds. Returning the transcript rather than raising lets the caller
        # salvage whatever was gathered.
        log.warning("tool loop hit max_rounds=%s without a final answer", max_rounds)
        return conversation, ""

    def list_models(self) -> list[str]:
        """Model ids the gateway advertises. Naming differs per gateway (OpenRouter uses
        `provider/model`, a bare Ollama uses `model:tag`), so this is for discovery and
        for validating a configured id rather than for guessing one."""
        try:
            return sorted(m.id for m in self._client.models.list().data)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list models from %s: %s", self.base_url, exc)
            return []
