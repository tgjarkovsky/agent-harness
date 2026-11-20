import asyncio
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, RateLimitEvent, ResultMessage, SystemMessage,
                              TextBlock, ToolResultBlock, ToolUseBlock, UserMessage, query)

from . import trace
from .graph import RateLimited

PROMPTS = Path(__file__).parent / "prompts"
READ_TOOLS = ["Read", "Glob", "Grep"]
WRITE_TOOLS = READ_TOOLS + ["Edit", "Write", "Bash"]
NO_PUBLISH = ["Bash(git push*)", "Bash(git commit*)", "Bash(gh *)"]
WORKER_APPEND = "You are a worker in an automated harness. Work only inside the current directory."


@dataclass
class WorkerResult:
    text: str
    usage: dict
    quota: float | None
    rate_limit: dict | None


def check_billing(billing: str, env: Mapping[str, str]) -> str | None:
    has_key = bool(env.get("ANTHROPIC_API_KEY"))
    if billing == "subscription" and has_key:
        return "billing=subscription but ANTHROPIC_API_KEY is set (would bill the API) — unset it"
    if billing == "api" and not has_key:
        return "billing=api but ANTHROPIC_API_KEY is not set"
    return None


def render_prompt(name: str, **fields) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8").format(**fields)


def _no_publish_env() -> dict[str, str]:
    """Env for workers: git cannot push and gh is logged out. Bash patterns alone are easy to bypass
    (`git -C . push`), and the harness pushes from its own process, not the worker's."""
    # ponytail: blocks origin + stored credentials; a push via ssh key to an explicit URL still works — sandbox in a later phase
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "remote.origin.pushurl", "GIT_CONFIG_VALUE_0": "file:///harness-push-disabled",  # fails at once; "x:" goes to ssh on POSIX
        "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GH_TOKEN": "",
        "GH_CONFIG_DIR": tempfile.mkdtemp(prefix="harness-gh-"),
    }


def worker_options(*, cwd, model: str, tools: list[str], disallowed=(),
                   permission_mode: str = "acceptEdits", max_turns: int = 60) -> ClaudeAgentOptions:
    # strict_mcp_config drops the account's claude.ai connectors (Gmail, Drive, Notion…) from workers
    return ClaudeAgentOptions(
        cwd=str(cwd), model=model, setting_sources=["project"], strict_mcp_config=True, env=_no_publish_env(),
        system_prompt={"type": "preset", "preset": "claude_code", "append": WORKER_APPEND},
        allowed_tools=list(tools), disallowed_tools=list(disallowed),
        permission_mode=permission_mode, max_turns=max_turns,
    )


def run_agent(prompt: str, *, cwd, model: str, tools: list[str], disallowed=(),
              permission_mode: str = "acceptEdits", max_turns: int = 60,
              supervise: Callable[[list[dict]], dict] | None = None) -> WorkerResult:
    """`supervise(steps)` returns SDK hooks; they see the call's steps and may append their own."""
    options = worker_options(cwd=cwd, model=model, tools=tools, disallowed=disallowed,
                             permission_mode=permission_mode, max_turns=max_turns)
    # read here, not inside the event loop: the trace context belongs to the calling graph node
    return asyncio.run(_collect(prompt, options, model, trace.current(), supervise))


def _rate_limit(info) -> dict:
    return {"type": info.rate_limit_type, "status": info.status,
            "utilization": info.utilization, "resets_at": info.resets_at}


@dataclass
class _Acc:
    started: float
    steps: list[dict] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    tool_names: dict[str, str] = field(default_factory=dict)
    result: ResultMessage | None = None
    rate_limit: dict | None = None
    billing: str | None = None


def _on_assistant(acc: _Acc, msg: AssistantMessage) -> None:
    for b in msg.content:
        if isinstance(b, TextBlock):
            acc.texts.append(b.text)
            acc.steps.append(trace.text_step(b.text))
        elif isinstance(b, ToolUseBlock):
            acc.tool_names[b.id] = b.name
            acc.steps.append(trace.tool_use_step(b.name, b.input))


def _on_user(acc: _Acc, msg: UserMessage) -> None:
    for b in msg.content if isinstance(msg.content, list) else []:
        if isinstance(b, ToolResultBlock):
            acc.steps.append(trace.tool_result_step(acc.tool_names.get(b.tool_use_id), b.content, b.is_error))


async def _consume(stream, acc: _Acc) -> None:
    async for msg in stream:
        if isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if info.rate_limit_type == "five_hour" or acc.rate_limit is None:
                acc.rate_limit = _rate_limit(info)
            if info.status == "rejected":
                raise RateLimited(info.resets_at)
        elif isinstance(msg, AssistantMessage):
            _on_assistant(acc, msg)
        elif isinstance(msg, UserMessage):
            _on_user(acc, msg)
        elif isinstance(msg, ResultMessage):
            acc.result = msg
        elif isinstance(msg, SystemMessage) and msg.subtype == "init":
            # "none" = logged in with the subscription; any key source bills the API for real
            source = msg.data.get("apiKeySource")
            acc.billing = None if source is None else "subscription" if source == "none" else "api"


def _tokens(result: ResultMessage | None) -> dict:
    u = (result.usage if result else None) or {}
    return {"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
            "cache_read_tokens": u.get("cache_read_input_tokens", 0),
            "cache_write_tokens": u.get("cache_creation_input_tokens", 0)}


def _finish(acc: _Acc, model: str) -> WorkerResult:
    result, rate_limit = acc.result, acc.rate_limit
    if result is None:
        raise RuntimeError("agent produced no result message")
    if result.api_error_status == 429:
        raise RateLimited(rate_limit and rate_limit["resets_at"])
    if result.is_error:
        raise RuntimeError(f"agent error: {result.subtype} {result.errors or ''}")
    usage = {"model": model, **_tokens(result), "cost_usd": result.total_cost_usd, "billing": acc.billing}
    text = result.result or (acc.texts[-1] if acc.texts else "")
    return WorkerResult(text, usage, rate_limit and rate_limit["utilization"], rate_limit)


def _call(t: trace.Trace, model: str, acc: _Acc, status: str, error: str | None) -> dict:
    r = acc.result
    return {"run_id": t.run_id, "node": t.node, "worker": t.worker, "model": model, "started": acc.started,
            "duration_ms": r.duration_ms if r else int((time.time() - acc.started) * 1000),
            "num_turns": r.num_turns if r else None, **_tokens(r),
            "cost_usd": r.total_cost_usd if r else None, "status": status, "error": error,
            "session_id": r.session_id if r else None, "billing": acc.billing}


async def _collect(prompt: str, options: ClaudeAgentOptions, model: str, t: trace.Trace,
                   supervise: Callable[[list[dict]], dict] | None = None) -> WorkerResult:
    """Every call is recorded — also failed and rate-limited ones, with the steps made so far."""
    acc, status, error = _Acc(time.time()), "error", "interrupted"  # Ctrl+C / cancel skip the excepts below
    if supervise:
        options.hooks = supervise(acc.steps)
    try:
        await _consume(query(prompt=prompt, options=options), acc)
        out = _finish(acc, model)
        status, error = "ok", None
        return out
    except RateLimited:
        status, error = "rate_limited", None
        raise
    except Exception as e:
        status, error = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        _record(_call(t, model, acc, status, error), acc.steps)


def _record(call: dict, steps: list[dict]) -> None:
    # bookkeeping must never turn a finished task into a failure or hide a rate-limit pause
    try:
        trace.emit(call, steps)
    except Exception as e:
        print(f"trace not recorded ({call.get('node')} {call.get('worker')}): {type(e).__name__}: {e}", file=sys.stderr)
