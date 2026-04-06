"""OpenAIEngine — OpenAI-compatible API engine with tool execution.

Use this engine for any provider that exposes an OpenAI-compatible
``/v1/chat/completions`` endpoint:

  - z.ai  (base_url: https://api.z.ai/api/paas/v4)
  - OpenAI API (GPT-4o, o3, o4-mini …)
  - Azure OpenAI
  - LiteLLM proxy (local or hosted)
  - Any other OpenAI-compatible endpoint

Configuration example (olamo-settings.json):

    {
      "agent_configs": {
        "lead-developer": {
          "engine": "openai",
          "model_config": {
            "mode": "advanced",
            "model": "z-ai/glm-5v-turbo",
            "base_url": "https://api.z.ai/api/paas/v4",
            "api_key": "YOUR_ZAI_API_KEY"
          }
        }
      }
    }

The engine runs a tool-call loop: after each model response it executes any
requested tool calls (using the same tool implementations as the Claude CLI
exposes) and feeds the results back into the conversation until the model
returns a final answer with no pending tool calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Awaitable, Callable

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore

from .base import AgentEngine
from ..models import AppSettings, ModelConfig


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling format)
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS: dict[str, dict] = {
    "Read": {
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read the full contents of a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Absolute or relative path to file"},
                },
                "required": ["file_path"],
            },
        },
    },
    "Write": {
        "type": "function",
        "function": {
            "name": "Write",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    "Edit": {
        "type": "function",
        "function": {
            "name": "Edit",
            "description": "Replace the first occurrence of old_str with new_str inside a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_str": {"type": "string", "description": "Exact string to find"},
                    "new_str": {"type": "string", "description": "Replacement string"},
                },
                "required": ["file_path", "old_str", "new_str"],
            },
        },
    },
    "Bash": {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Execute a shell command and return its stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "default": 120, "description": "Timeout in seconds"},
                },
                "required": ["command"],
            },
        },
    },
    "Glob": {
        "type": "function",
        "function": {
            "name": "Glob",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py'"},
                    "path": {"type": "string", "default": ".", "description": "Root directory to search"},
                },
                "required": ["pattern"],
            },
        },
    },
    "Grep": {
        "type": "function",
        "function": {
            "name": "Grep",
            "description": "Search for a regex pattern in files. Returns matching lines with file:line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "include": {"type": "string", "default": "", "description": "Glob filter, e.g. '*.py'"},
                },
                "required": ["pattern"],
            },
        },
    },
    "WebFetch": {
        "type": "function",
        "function": {
            "name": "WebFetch",
            "description": "Fetch the text content of a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    "WebSearch": {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "Search the web for information using DuckDuckGo.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
}


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

# Cap individual tool results so that a single large file read can't blow the
# conversation history budget.  z.ai's coding plan API rejects requests when
# the accumulated JSON body grows too large (error 1261).  50 000 chars ≈ 12K
# tokens — plenty for any reviewer to work with, but safe for the API.
_MAX_TOOL_RESULT_CHARS = 50_000


def _trim_tool_result(result: str) -> str:
    if len(result) <= _MAX_TOOL_RESULT_CHARS:
        return result
    keep = _MAX_TOOL_RESULT_CHARS - 120
    return (
        result[:keep]
        + f"\n\n… [output truncated at {_MAX_TOOL_RESULT_CHARS} chars; "
        f"{len(result) - keep} chars omitted]"
    )


async def _run_tool(name: str, args: dict) -> str:
    """Execute a tool call and return a string result."""
    try:
        if name == "Read":
            p = Path(args["file_path"])
            if not p.exists():
                return f"Error: file not found: {args['file_path']}"
            return _trim_tool_result(p.read_text(errors="replace"))

        elif name == "Write":
            p = Path(args["file_path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(args["content"])
            return f"Wrote {len(args['content'])} chars to {args['file_path']}"

        elif name == "Edit":
            p = Path(args["file_path"])
            if not p.exists():
                return f"Error: file not found: {args['file_path']}"
            text = p.read_text(errors="replace")
            if args["old_str"] not in text:
                return f"Error: old_str not found in {args['file_path']}"
            p.write_text(text.replace(args["old_str"], args["new_str"], 1))
            return f"Edited {args['file_path']}"

        elif name == "Bash":
            cmd = args["command"]
            timeout = int(args.get("timeout", 120))
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                out = stdout.decode(errors="replace")
                return _trim_tool_result(f"Exit {proc.returncode}\n{out}" if proc.returncode else out)
            except asyncio.TimeoutError:
                proc.kill()
                return f"Error: timed out after {timeout}s"

        elif name == "Glob":
            import glob as _glob
            base = args.get("path", ".")
            pat = args["pattern"]
            full = str(Path(base) / pat) if not pat.startswith("/") else pat
            matches = sorted(_glob.glob(full, recursive=True))
            return _trim_tool_result("\n".join(matches) if matches else "(no matches)")

        elif name == "Grep":
            path = args.get("path", ".")
            include = args.get("include", "")
            cmd = ["grep", "-rn"]
            if include:
                cmd += ["--include", include]
            cmd += [args["pattern"], path]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return _trim_tool_result(r.stdout.strip() or "(no matches)")

        elif name == "WebFetch":
            import urllib.request
            with urllib.request.urlopen(args["url"], timeout=30) as resp:
                return resp.read().decode(errors="replace")[:8000]

        elif name == "WebSearch":
            import urllib.parse, urllib.request
            q = urllib.parse.quote_plus(args["query"])
            url = f"https://api.duckduckgo.com/?q={q}&format=json&no_redirect=1"
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read())
            abstract = data.get("AbstractText") or data.get("Answer") or ""
            related = [t.get("Text", "") for t in data.get("RelatedTopics", [])[:5]]
            return "\n".join(filter(None, [abstract] + related)) or "(no results)"

        else:
            return f"Unknown tool: {name}"

    except Exception as exc:
        return f"Tool error ({name}): {exc}"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class OpenAIEngine:
    """
    OpenAI-compatible API engine.

    Supports any provider with an OpenAI-compatible ``/v1/chat/completions``
    endpoint.  Set ``engine: "openai"`` in ``AgentEngineConfig`` and provide
    ``model_config.base_url`` + ``model_config.api_key`` in advanced mode.

    For a simple OpenAI API call (no custom endpoint) just set
    ``model_config.mode = "simple"`` and ensure ``OPENAI_API_KEY`` is set.
    """

    MAX_TOOL_ITERATIONS = 50
    # Default max response tokens — keeps outputs focused and avoids hitting
    # context limits on models with tighter prompt+completion budgets (e.g. GLM).
    DEFAULT_MAX_TOKENS = 2048

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings

    async def start(self) -> None:
        if AsyncOpenAI is None:
            raise SystemExit(
                "openai package not installed and the OpenAI engine is required.\n"
                "Install with: pip install openai>=1.0"
            )

    async def stop(self) -> None:
        pass

    def _client(self, model_config: ModelConfig) -> "AsyncOpenAI":
        api_key = (
            model_config.api_key
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("Z_AI_API_KEY")
            or "sk-placeholder"
        )
        base_url = model_config.base_url or None
        return AsyncOpenAI(api_key=api_key, base_url=base_url)

    # Total character budget for the messages list before compaction kicks in.
    # Keeps the JSON request body safely under z.ai's coding plan API size limit.
    MAX_HISTORY_CHARS = 300_000

    def _compact_history(self, messages: list[dict]) -> list[dict]:
        """Drop the oldest tool-result messages until total history fits budget."""
        total = sum(len(json.dumps(m)) for m in messages)
        if total <= self.MAX_HISTORY_CHARS:
            return messages
        # Always keep system (index 0) and user (index 1); trim from index 2 onward
        trimmed = list(messages)
        i = 2
        while i < len(trimmed) and sum(len(json.dumps(m)) for m in trimmed) > self.MAX_HISTORY_CHARS:
            if trimmed[i].get("role") in ("tool", "assistant"):
                trimmed.pop(i)
            else:
                i += 1
        return trimmed

    async def run(
        self,
        role: str,
        prompt: str,
        system_prompt: str,
        tools: list[str],
        model: str,
        model_config: ModelConfig,
        mcp_servers: dict[str, dict],
        on_event: Callable[[dict], Awaitable[None]],
    ) -> str:
        client = self._client(model_config)
        tool_schemas = [_TOOL_SCHEMAS[t] for t in tools if t in _TOOL_SCHEMAS]

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        result = ""
        for _ in range(self.MAX_TOOL_ITERATIONS):
            messages = self._compact_history(messages)
            kwargs: dict = {"model": model, "messages": messages}
            if tool_schemas:
                kwargs["tools"] = tool_schemas
                kwargs["tool_choice"] = "auto"
            # Apply max_tokens unless caller explicitly sets it via extra_params
            if "max_tokens" not in (model_config.extra_params or {}):
                kwargs["max_tokens"] = self.DEFAULT_MAX_TOKENS
            if model_config.extra_params:
                kwargs.update(model_config.extra_params)

            response = await client.chat.completions.create(**kwargs)
            msg = response.choices[0].message

            # Build assistant entry for conversation history
            entry: dict = {"role": "assistant"}
            if msg.content:
                entry["content"] = msg.content
                result = msg.content
                # Emit full content — no truncation so log files get everything
                await on_event({"type": "agent_message", "role": role, "text": msg.content})
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(entry)

            if not msg.tool_calls:
                break

            for tc in msg.tool_calls:
                try:
                    tc_args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tc_args = {}
                # Emit tool call event so logs and UI show what the agent is doing
                args_preview = json.dumps(tc_args)[:200]
                await on_event({
                    "type": "agent_tool_call",
                    "role": role,
                    "tool_name": tc.function.name,
                    "args_preview": args_preview,
                })
                tool_result = await _run_tool(tc.function.name, tc_args)
                # Emit tool result event (truncated for SSE, full content in log file)
                await on_event({
                    "type": "agent_tool_result",
                    "role": role,
                    "tool_name": tc.function.name,
                    "result_preview": tool_result[:500],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

        return result
