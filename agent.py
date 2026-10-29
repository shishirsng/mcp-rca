#!/usr/bin/env python3
"""RepoSleuth agent: CLI debugging agent using MCP tools.

Uses LangChain with MultiServerMCPClient to launch the MCP server and
investigate repository bugs via tool-calling. Writes structured JSONL traces.
Powered by OpenRouter.

Usage:
    python agent.py "Why are users getting 401 after login?"
    python agent.py --model openrouter/auto "Why are users getting 401 after login?"
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Ensure UTF-8 output encoding on Windows consoles
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain.agents import create_agent

from prompts import SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRACES_DIR = Path(__file__).parent / "traces"
TRACES_DIR.mkdir(exist_ok=True)

RECURSION_LIMIT = 30
TOTAL_TIMEOUT_SECONDS = 300
SERVER_SCRIPT = str(Path(__file__).parent / "server.py")


def get_python_executable() -> str:
    """Get the current Python executable path."""
    return sys.executable


def get_llm(model_name: str = "openrouter"):
    """Create the LLM instance using OpenRouter.

    Uses OpenRouter API via OPENROUTER_API_KEY environment variable (or .env file).
    Default model is 'openrouter/auto' (or user-selected model via OPENROUTER_MODEL).
    """
    from langchain_openai import ChatOpenAI

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise ValueError(
            "Missing OPENROUTER_API_KEY in .env file or environment.\n"
            "Add your key to the .env file:\n"
            "  OPENROUTER_API_KEY=sk-or-v1-...\n"
            "Get an API key at https://openrouter.ai"
        )

    # Determine model ID
    if model_name in ("openrouter", "default", "auto", "model:auto"):
        or_model = os.environ.get("OPENROUTER_MODEL", "openrouter/auto").strip()
    else:
        or_model = model_name.strip()

    # Normalize "auto" or "model:auto" to "openrouter/auto"
    if or_model in ("auto", "model:auto"):
        or_model = "openrouter/auto"

    return ChatOpenAI(
        model=or_model,
        openai_api_key=api_key,
        openai_api_base="https://openrouter.ai/api/v1",
        temperature=0,
        streaming=True,
        default_headers={
            "HTTP-Referer": "https://github.com/reposleuth",
            "X-Title": "RepoSleuth",
        },
    )


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


class Tracer:
    """Records tool calls and agent steps to a JSONL file."""

    def __init__(self, run_id: str, scenario_id: str = "interactive"):
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.entries: list[dict] = []
        self.trace_file = TRACES_DIR / f"{scenario_id}_{run_id}.jsonl"

    def record(self, **kwargs) -> None:
        """Record a trace entry."""
        entry = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        self.entries.append(entry)

    def save(self) -> Path:
        """Write all entries to the JSONL file."""
        with open(self.trace_file, "w", encoding="utf-8") as f:
            for entry in self.entries:
                f.write(json.dumps(entry, default=str) + "\n")
        return self.trace_file


# ---------------------------------------------------------------------------
# Answer validation
# ---------------------------------------------------------------------------


def parse_json_answer(text: str) -> dict | None:
    """Extract JSON from the agent's final response."""
    if not text:
        return None

    # Try to find ```json ... ``` blocks first
    json_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1).strip())
        except (json.JSONDecodeError, ValueError):
            pass

    # Try to find outer-most JSON object { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            pass

    # Try the whole text directly
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass

    return None


def validate_commit_in_trace(answer: dict, tracer: Tracer) -> bool:
    """Verify that the cited commit hash exists in the tool output trace."""
    cited_commit = answer.get("commit", "")
    if not cited_commit or cited_commit == "unknown":
        return True  # no commit cited, nothing to validate

    # Search trace entries for the cited commit
    for entry in tracer.entries:
        result = entry.get("result", "")
        if isinstance(result, str) and cited_commit[:8] in result:
            return True
    return False


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------


async def run_agent(
    question: str,
    model_name: str = "openrouter",
    scenario_id: str = "interactive",
    run_id: str | None = None,
) -> dict:
    """Run the RepoSleuth agent on a question.

    Returns a dict with: answer, tool_calls, latency, trace_file, error
    """
    if run_id is None:
        run_id = uuid.uuid4().hex[:8]

    tracer = Tracer(run_id, scenario_id)
    tracer.record(event="start", question=question, model=model_name)

    result_data = {
        "answer": None,
        "tool_calls": 0,
        "duplicate_calls": 0,
        "latency": 0.0,
        "trace_file": "",
        "error": None,
    }

    start_time = time.time()

    try:
        llm = get_llm(model_name)
        python_exe = get_python_executable()
        repo_root = str((Path(__file__).parent / "fixture_repo").resolve())

        client = MultiServerMCPClient(
            {
                "reposleuth": {
                    "command": python_exe,
                    "args": [SERVER_SCRIPT],
                    "transport": "stdio",
                    "env": {"REPO_ROOT": repo_root},
                }
            }
        )
        tools = await client.get_tools()
        agent = create_agent(llm, tools, system_prompt=SYSTEM_PROMPT)

        messages = [
            HumanMessage(content=question),
        ]

        seen_calls: list[tuple[str, str]] = []
        all_messages: list = []
        final_text = ""

        async def _stream_execution():
            nonlocal final_text
            async for event in agent.astream(
                {"messages": messages},
                stream_mode="updates",
                config={"recursion_limit": RECURSION_LIMIT},
            ):
                for node_name, node_output in event.items():
                    node_msgs = node_output.get("messages", [])
                    for msg in node_msgs:
                        all_messages.append(msg)
                        if isinstance(msg, AIMessage) and msg.tool_calls:
                            for tc in msg.tool_calls:
                                call_key = (tc["name"], json.dumps(tc.get("args", {}), sort_keys=True))
                                if call_key in seen_calls:
                                    result_data["duplicate_calls"] += 1
                                seen_calls.append(call_key)
                                result_data["tool_calls"] += 1

                                tracer.record(
                                    event="tool_call",
                                    tool=tc["name"],
                                    args=tc.get("args", {}),
                                )
                        elif isinstance(msg, ToolMessage):
                            content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                            tool_name = getattr(msg, "name", "tool") or "tool"
                            tracer.record(
                                event="tool_result",
                                tool=tool_name,
                                result=content_str[:2000],  # limit trace size
                                result_size=len(content_str),
                            )
                        elif isinstance(msg, AIMessage) and not msg.tool_calls and msg.content:
                            final_text = msg.content

        try:
            await asyncio.wait_for(_stream_execution(), timeout=TOTAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            result_data["error"] = "timeout"
            tracer.record(event="error", error="timeout")
        except Exception as e:
            result_data["error"] = f"crash: {e}"
            tracer.record(event="error", error=str(e))

        answer = parse_json_answer(final_text)

        if answer:
            # Validate cited commit
            if not validate_commit_in_trace(answer, tracer):
                tracer.record(
                    event="validation_warning",
                    message="Cited commit not found in tool output.",
                )
            result_data["answer"] = answer
            if result_data["error"] and "Recursion" in str(result_data["error"]):
                result_data["error"] = None
        elif not result_data.get("error"):
            result_data["answer"] = {"raw_text": final_text}
            result_data["error"] = "invalid_json"

    except Exception as e:
        result_data["error"] = f"crash: {e}"
        tracer.record(event="error", error=str(e))

    result_data["latency"] = round(time.time() - start_time, 2)
    tracer.record(event="finish", **result_data)
    trace_path = tracer.save()
    result_data["trace_file"] = str(trace_path)

    return result_data


# ---------------------------------------------------------------------------
# No-tools baseline
# ---------------------------------------------------------------------------


async def run_baseline(question: str, model_name: str = "openrouter") -> dict:
    """Run the same model without tools (baseline for comparison)."""
    start_time = time.time()

    try:
        llm = get_llm(model_name)
        response = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=question),
            ]),
            timeout=TOTAL_TIMEOUT_SECONDS,
        )
        answer = parse_json_answer(response.content)
        return {
            "answer": answer or {"raw_text": response.content},
            "tool_calls": 0,
            "latency": round(time.time() - start_time, 2),
            "error": None if answer else "invalid_json",
        }
    except asyncio.TimeoutError:
        return {"answer": None, "tool_calls": 0, "latency": TOTAL_TIMEOUT_SECONDS, "error": "timeout"}
    except Exception as e:
        return {"answer": None, "tool_calls": 0, "latency": round(time.time() - start_time, 2), "error": str(e)}


# ---------------------------------------------------------------------------
# Raw agent loop (for understanding the mechanics)
# ---------------------------------------------------------------------------


async def raw_agent_loop(question: str, model_name: str = "openrouter") -> None:
    """Demonstrate the raw agent loop without LangGraph.

    This shows the underlying process:
    1. Call model
    2. Receive tool request
    3. Execute the requested tool
    4. Append the result
    5. Repeat
    """
    print("=" * 60)
    print("RAW AGENT LOOP (for understanding the mechanics)")
    print("=" * 60)

    try:
        llm = get_llm(model_name)
    except ValueError as e:
        print(f"\n[Error] {e}", file=sys.stderr)
        return
    python_exe = get_python_executable()
    repo_root = str((Path(__file__).parent / "fixture_repo").resolve())

    client = MultiServerMCPClient(
        {
            "reposleuth": {
                "command": python_exe,
                "args": [SERVER_SCRIPT],
                "transport": "stdio",
                "env": {"REPO_ROOT": repo_root},
            }
        }
    )
    tools = await client.get_tools()

    # Bind tools to the model
    model_with_tools = llm.bind_tools(tools)

    # Build initial messages
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=question),
    ]

    # Create a tool name -> tool function mapping
    tool_map = {tool.name: tool for tool in tools}

    step = 0
    max_steps = min(RECURSION_LIMIT, 5)

    while step < max_steps:
        step += 1
        print(f"\n--- Step {step} ---", flush=True)

        # 1. Call model
        response = await model_with_tools.ainvoke(messages)
        messages.append(response)

        # 2. Check if model wants to call tools
        if not response.tool_calls:
            print(f"[FINAL ANSWER]\n{response.content}", flush=True)
            break

        # 3. Execute each requested tool
        for tc in response.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]
            print(f"[TOOL CALL] {tool_name}({json.dumps(tool_args)})", flush=True)

            if tool_name not in tool_map:
                result = f"Error: unknown tool '{tool_name}'"
            else:
                # Execute the tool
                result = await tool_map[tool_name].ainvoke(tool_args)

            print(f"[RESULT] {str(result)[:200]}...", flush=True)

            # 4. Append the result as a ToolMessage
            messages.append(
                ToolMessage(content=str(result), tool_call_id=tc["id"])
            )

        # 5. Loop continues - model will process results and decide next step

    if step >= max_steps:
        print(f"\n[STOPPED] Reached recursion limit ({max_steps})", flush=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="RepoSleuth: Evidence-Based Debugging Agent using MCP"
    )
    parser.add_argument("question", nargs="?", default="Why are users getting 401 after login?",
                       help="The debugging question to investigate")
    parser.add_argument("--model", default="openrouter",
                       help="Model: openrouter (default, uses free tier), or model name (e.g. meta-llama/llama-3.3-70b-instruct:free)")
    parser.add_argument("--raw", action="store_true",
                       help="Run the raw agent loop (for learning)")
    args = parser.parse_args()

    try:
        if args.raw:
            asyncio.run(raw_agent_loop(args.question, args.model))
        else:
            result = asyncio.run(run_agent(args.question, args.model))
            print("\n" + "=" * 60)
            print("RESULT")
            print("=" * 60)
            print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        if not isinstance(e, (KeyboardInterrupt, SystemExit)):
            print(f"\n[Error] {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
