#!/usr/bin/env python3
"""RepoSleuth agent: CLI debugging agent using MCP tools.

Uses LangChain with MultiServerMCPClient to launch the MCP server and
investigate repository bugs via tool-calling. Writes structured JSONL traces.

Usage:
    python agent.py "Why are users getting 401 after login?"
    python agent.py --model gemini "Why are users getting 401 after login?"
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

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from prompts import SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TRACES_DIR = Path(__file__).parent / "traces"
TRACES_DIR.mkdir(exist_ok=True)

RECURSION_LIMIT = 25
TOTAL_TIMEOUT_SECONDS = 120
SERVER_SCRIPT = str(Path(__file__).parent / "server.py")


def get_python_executable() -> str:
    """Get the current Python executable path."""
    return sys.executable


def get_llm(model_name: str = "ollama"):
    """Create the LLM instance based on model name.

    Supports:
        - 'ollama' or model name like 'qwen2.5': uses Ollama local model
        - 'gemini': uses Google Gemini via API key in GOOGLE_API_KEY env var
    """
    if model_name in ("gemini", "gemini-2.0-flash"):
        from langchain_google_genai import ChatGoogleGenerativeAI
        api_key = os.environ.get("GOOGLE_API_KEY", "")
        if not api_key:
            raise ValueError("Set GOOGLE_API_KEY environment variable for Gemini.")
        return ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",
            google_api_key=api_key,
            temperature=0,
        )
    else:
        from langchain_ollama import ChatOllama
        # Default to qwen2.5 if just 'ollama' specified
        ollama_model = model_name if model_name != "ollama" else "qwen2.5"
        return ChatOllama(model=ollama_model, temperature=0)


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
    # Try to find JSON block in the text
    # Look for ```json ... ``` blocks first
    json_match = re.search(r"```json\s*\n?(.*?)\n?```", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find a raw JSON object
    json_match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    # Try the whole text
    try:
        return json.loads(text)
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
    model_name: str = "ollama",
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

    llm = get_llm(model_name)
    python_exe = get_python_executable()
    repo_root = str((Path(__file__).parent / "fixture_repo").resolve())

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
        async with MultiServerMCPClient(
            {
                "reposleuth": {
                    "command": python_exe,
                    "args": [SERVER_SCRIPT],
                    "transport": "stdio",
                    "env": {"REPO_ROOT": repo_root},
                }
            }
        ) as client:
            tools = client.get_tools()
            agent = create_react_agent(llm, tools)

            messages = [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=question),
            ]

            # Track tool calls for deduplication counting
            seen_calls: list[tuple[str, str]] = []

            response = await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": messages},
                    config={"recursion_limit": RECURSION_LIMIT},
                ),
                timeout=TOTAL_TIMEOUT_SECONDS,
            )

            # Process response messages to count tool calls and extract answer
            for msg in response.get("messages", []):
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

                if isinstance(msg, ToolMessage):
                    content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                    tracer.record(
                        event="tool_result",
                        tool=msg.name,
                        result=content_str[:2000],  # limit trace size
                        result_size=len(content_str),
                    )

            # Extract final answer from the last AI message
            final_text = ""
            for msg in reversed(response.get("messages", [])):
                if isinstance(msg, AIMessage) and not msg.tool_calls:
                    final_text = msg.content
                    break

            answer = parse_json_answer(final_text)

            if answer:
                # Validate cited commit
                if not validate_commit_in_trace(answer, tracer):
                    tracer.record(
                        event="validation_warning",
                        message="Cited commit not found in tool output. Requesting correction.",
                    )
                    # Send correction message
                    correction_messages = response["messages"] + [
                        HumanMessage(
                            content=(
                                "The commit hash you cited was not found in any tool output. "
                                "Please re-check by using git_log and provide a correct commit hash."
                            )
                        ),
                    ]
                    correction_response = await asyncio.wait_for(
                        agent.ainvoke(
                            {"messages": correction_messages},
                            config={"recursion_limit": 10},
                        ),
                        timeout=60,
                    )
                    for msg in reversed(correction_response.get("messages", [])):
                        if isinstance(msg, AIMessage) and not msg.tool_calls:
                            corrected = parse_json_answer(msg.content)
                            if corrected:
                                answer = corrected
                            break

                result_data["answer"] = answer
            else:
                result_data["answer"] = {"raw_text": final_text}
                result_data["error"] = "invalid_json"

    except asyncio.TimeoutError:
        result_data["error"] = "timeout"
        tracer.record(event="error", error="timeout")
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


async def run_baseline(question: str, model_name: str = "ollama") -> dict:
    """Run the same model without tools (baseline for comparison)."""
    llm = get_llm(model_name)
    start_time = time.time()

    try:
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


async def raw_agent_loop(question: str, model_name: str = "ollama") -> None:
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

    llm = get_llm(model_name)
    python_exe = get_python_executable()
    repo_root = str((Path(__file__).parent / "fixture_repo").resolve())

    async with MultiServerMCPClient(
        {
            "reposleuth": {
                "command": python_exe,
                "args": [SERVER_SCRIPT],
                "transport": "stdio",
                "env": {"REPO_ROOT": repo_root},
            }
        }
    ) as client:
        tools = client.get_tools()

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
        max_steps = RECURSION_LIMIT

        while step < max_steps:
            step += 1
            print(f"\n--- Step {step} ---")

            # 1. Call model
            response = await model_with_tools.ainvoke(messages)
            messages.append(response)

            # 2. Check if model wants to call tools
            if not response.tool_calls:
                print(f"[FINAL ANSWER]\n{response.content}")
                break

            # 3. Execute each requested tool
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_args = tc["args"]
                print(f"[TOOL CALL] {tool_name}({json.dumps(tool_args)})")

                if tool_name not in tool_map:
                    result = f"Error: unknown tool '{tool_name}'"
                else:
                    # Execute the tool
                    result = await tool_map[tool_name].ainvoke(tool_args)

                print(f"[RESULT] {str(result)[:200]}...")

                # 4. Append the result as a ToolMessage
                messages.append(
                    ToolMessage(content=str(result), tool_call_id=tc["id"])
                )

            # 5. Loop continues - model will process results and decide next step

        if step >= max_steps:
            print(f"\n[STOPPED] Reached recursion limit ({max_steps})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="RepoSleuth: Evidence-Based Debugging Agent"
    )
    parser.add_argument("question", nargs="?", default="Why are users getting 401 after login?",
                       help="The debugging question to investigate")
    parser.add_argument("--model", default="gemini",
                       help="Model to use: ollama, qwen2.5, gemini (default: gemini)")
    parser.add_argument("--raw", action="store_true",
                       help="Run the raw agent loop (for learning)")
    args = parser.parse_args()

    if args.raw:
        asyncio.run(raw_agent_loop(args.question, args.model))
    else:
        result = asyncio.run(run_agent(args.question, args.model))
        print("\n" + "=" * 60)
        print("RESULT")
        print("=" * 60)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
