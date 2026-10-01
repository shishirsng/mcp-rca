#!/usr/bin/env python3
"""Evaluation harness for RepoSleuth.

Builds the fixture repository, runs the agent on each scenario 3 times,
runs a no-tools baseline, scores correctness, and saves results.

Usage:
    python run_eval.py
    python run_eval.py --model gemini
    python run_eval.py --runs 1  # quick test with 1 run per scenario
"""

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from agent import run_agent, run_baseline

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def build_fixture() -> None:
    """Run make_fixture.py to build the fixture repository."""
    print("=" * 60)
    print("Step 1: Building fixture repository...")
    print("=" * 60)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "make_fixture.py")],
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
        timeout=60,
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"ERROR building fixture: {result.stderr}", file=sys.stderr)
        sys.exit(1)


def load_scenarios() -> list[dict]:
    """Load all scenario YAML files."""
    scenarios = []
    for yaml_file in sorted(SCENARIOS_DIR.glob("*.yaml")):
        with open(yaml_file, encoding="utf-8") as f:
            scenario = yaml.safe_load(f)
        scenario["_file"] = yaml_file.name
        scenarios.append(scenario)
    return scenarios


def score_answer(answer: dict | None, truth: dict, scenario_id: str) -> bool:
    """Score an agent answer against the ground truth.

    For normal scenarios: correct if answer names the correct file AND commit
    OR matches the specified keywords.

    For unanswerable scenario: correct only if agent abstains.
    """
    if answer is None:
        return False

    # Unanswerable scenario
    if scenario_id == "unanswerable":
        root_cause = str(answer.get("root_cause", "")).lower()
        keywords = truth.get("keywords", [])
        for kw in keywords:
            if kw.lower() in root_cause:
                return True
        # Also check if confidence is low and no specific file/commit cited
        if answer.get("confidence") == "low" and answer.get("commit") in (None, "unknown", ""):
            return True
        return False

    # Normal scenarios
    truth_file = truth.get("file", "")
    truth_commit = truth.get("commit", "")
    truth_keywords = truth.get("keywords", [])

    answer_files = answer.get("files", [])
    answer_commit = str(answer.get("commit", ""))
    answer_root_cause = str(answer.get("root_cause", "")).lower()
    answer_evidence = " ".join(str(e) for e in answer.get("evidence", [])).lower()

    # Check file match
    file_match = any(truth_file in f for f in answer_files) if truth_file else False

    # Check commit match (first 8 chars)
    commit_match = False
    if truth_commit and answer_commit:
        commit_match = (
            answer_commit[:8] == truth_commit[:8]
            or truth_commit[:8] in answer_commit
            or answer_commit in truth_commit
        )

    # Check keyword match
    keyword_match = False
    combined_text = answer_root_cause + " " + answer_evidence
    matched_keywords = sum(1 for kw in truth_keywords if kw.lower() in combined_text)
    if matched_keywords >= 2:  # At least 2 keyword matches
        keyword_match = True

    # Correct if file+commit match, or if keywords match well
    return (file_match and commit_match) or (file_match and keyword_match) or (commit_match and keyword_match)


async def evaluate_scenario(
    scenario: dict,
    model_name: str,
    num_runs: int = 3,
) -> dict:
    """Evaluate a single scenario with multiple runs."""
    sid = scenario["id"]
    question = scenario["question"]
    truth = scenario["truth"]

    print(f"\n  Scenario: {sid}")
    print(f"  Question: {question}")

    # Agent runs
    agent_results = []
    for run_idx in range(num_runs):
        print(f"    Agent run {run_idx + 1}/{num_runs}...", end=" ", flush=True)
        try:
            result = await run_agent(
                question=question,
                model_name=model_name,
                scenario_id=sid,
                run_id=f"run{run_idx + 1}",
            )
            correct = score_answer(result.get("answer"), truth, sid)
            result["correct"] = correct
            agent_results.append(result)
            status = "CORRECT" if correct else "WRONG"
            print(f"{status} (tools: {result['tool_calls']}, "
                  f"latency: {result['latency']}s, "
                  f"error: {result.get('error', 'none')})")
        except Exception as e:
            print(f"CRASH: {e}")
            agent_results.append({
                "answer": None, "tool_calls": 0, "latency": 0,
                "error": f"crash: {e}", "correct": False,
            })

    # Baseline runs (no tools)
    baseline_results = []
    for run_idx in range(num_runs):
        print(f"    Baseline run {run_idx + 1}/{num_runs}...", end=" ", flush=True)
        try:
            result = await run_baseline(question, model_name)
            correct = score_answer(result.get("answer"), truth, sid)
            result["correct"] = correct
            baseline_results.append(result)
            status = "CORRECT" if correct else "WRONG"
            print(f"{status}")
        except Exception as e:
            print(f"CRASH: {e}")
            baseline_results.append({
                "answer": None, "tool_calls": 0, "latency": 0,
                "error": f"crash: {e}", "correct": False,
            })

    agent_correct = sum(1 for r in agent_results if r.get("correct"))
    baseline_correct = sum(1 for r in baseline_results if r.get("correct"))
    avg_tool_calls = (
        sum(r.get("tool_calls", 0) for r in agent_results) / len(agent_results)
        if agent_results else 0
    )
    avg_latency = (
        sum(r.get("latency", 0) for r in agent_results) / len(agent_results)
        if agent_results else 0
    )
    duplicate_calls = sum(r.get("duplicate_calls", 0) for r in agent_results)
    failures = sum(1 for r in agent_results if r.get("error"))

    return {
        "scenario_id": sid,
        "question": question,
        "agent_correct": agent_correct,
        "baseline_correct": baseline_correct,
        "num_runs": num_runs,
        "avg_tool_calls": round(avg_tool_calls, 1),
        "avg_latency": round(avg_latency, 1),
        "duplicate_calls": duplicate_calls,
        "failures": failures,
        "agent_results": agent_results,
        "baseline_results": baseline_results,
    }


def save_results(
    all_results: list[dict],
    model_name: str,
    eval_date: str,
) -> Path:
    """Save results to results/results.md."""
    output_path = RESULTS_DIR / "results.md"

    lines = [
        "# RepoSleuth Evaluation Results\n",
        f"**Model**: {model_name}  \n",
        f"**Date**: {eval_date}  \n",
        f"**Runs per scenario**: {all_results[0]['num_runs'] if all_results else 'N/A'}  \n",
        "",
        "## Results Table\n",
        "| Scenario | Agent (N runs) | No-tools baseline | Avg tool calls | Avg latency (s) | Failures |",
        "| -------- | -------------- | ----------------- | -------------- | ---------------- | -------- |",
    ]

    total_agent = 0
    total_baseline = 0
    total_runs = 0

    for r in all_results:
        n = r["num_runs"]
        total_agent += r["agent_correct"]
        total_baseline += r["baseline_correct"]
        total_runs += n
        lines.append(
            f"| {r['scenario_id']} | {r['agent_correct']}/{n} "
            f"| {r['baseline_correct']}/{n} "
            f"| {r['avg_tool_calls']} "
            f"| {r['avg_latency']} "
            f"| {r['failures']} |"
        )

    lines.append(
        f"| **Total** | **{total_agent}/{total_runs}** "
        f"| **{total_baseline}/{total_runs}** "
        f"| - | - | - |"
    )

    lines.append("")
    lines.append("## Detailed Results\n")

    for r in all_results:
        lines.append(f"### {r['scenario_id']}\n")
        lines.append(f"**Question**: {r['question']}\n")
        for i, ar in enumerate(r.get("agent_results", [])):
            status = "✓" if ar.get("correct") else "✗"
            answer = ar.get("answer", {})
            root_cause = answer.get("root_cause", "N/A") if isinstance(answer, dict) else "N/A"
            lines.append(
                f"- Run {i+1}: {status} | tools: {ar.get('tool_calls', 0)} | "
                f"root_cause: {root_cause[:80]}"
            )
        lines.append("")

    # Find a failed run for the README
    lines.append("## Example Failed Run\n")
    failed_run = None
    for r in all_results:
        for ar in r.get("agent_results", []):
            if not ar.get("correct"):
                failed_run = {"scenario": r["scenario_id"], "result": ar}
                break
        if failed_run:
            break

    if failed_run:
        lines.append(f"**Scenario**: {failed_run['scenario']}\n")
        lines.append("```json")
        lines.append(json.dumps(failed_run["result"].get("answer", {}), indent=2, default=str))
        lines.append("```\n")
        lines.append(
            f"**Analysis**: The agent {'timed out' if failed_run['result'].get('error') == 'timeout' else 'did not find the correct root cause'}. "
            f"Error: {failed_run['result'].get('error', 'none')}\n"
        )
    else:
        lines.append("All runs passed.\n")

    content = "\n".join(lines) + "\n"
    output_path.write_text(content, encoding="utf-8")
    print(f"\nResults saved to: {output_path}")
    return output_path


async def run_evaluation(model_name: str, num_runs: int = 3) -> None:
    """Run the complete evaluation pipeline."""
    # Step 1: Build fixture
    build_fixture()

    # Step 2: Load scenarios
    scenarios = load_scenarios()
    print(f"\nLoaded {len(scenarios)} scenarios")

    # Step 3: Evaluate each scenario
    print("\n" + "=" * 60)
    print(f"Step 2: Running evaluation ({num_runs} runs per scenario)...")
    print("=" * 60)

    all_results = []
    for scenario in scenarios:
        result = await evaluate_scenario(scenario, model_name, num_runs)
        all_results.append(result)

    # Step 4: Print summary
    print("\n" + "=" * 60)
    print("EVALUATION SUMMARY")
    print("=" * 60)

    eval_date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    print(f"\nModel: {model_name}")
    print(f"Date: {eval_date}")
    print(f"{'Scenario':<20} {'Agent':>12} {'Baseline':>12} {'Avg Tools':>12}")
    print("-" * 60)

    for r in all_results:
        n = r["num_runs"]
        print(
            f"{r['scenario_id']:<20} "
            f"{r['agent_correct']}/{n:>10} "
            f"{r['baseline_correct']}/{n:>10} "
            f"{r['avg_tool_calls']:>12}"
        )

    # Step 5: Save results
    save_results(all_results, model_name, eval_date)


def main():
    parser = argparse.ArgumentParser(description="RepoSleuth Evaluation Harness")
    parser.add_argument("--model", default="gemini",
                       help="Model: ollama, qwen2.5, gemini (default: gemini)")
    parser.add_argument("--runs", type=int, default=3,
                       help="Number of runs per scenario (default: 3)")
    args = parser.parse_args()

    asyncio.run(run_evaluation(args.model, args.runs))


if __name__ == "__main__":
    main()
