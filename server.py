#!/usr/bin/env python3
"""MCP server exposing read-only repository investigation tools.

Runs over JSON-RPC via stdio. Exposes five tools:
  - list_files: List tracked files in the repository
  - search_code: Regex search across tracked files (via ripgrep)
  - read_file: Read a file with path-traversal protection
  - git_log: View commit history
  - git_show: View a specific commit's diff

All tools are read-only and enforce a path jail to prevent traversal attacks.
"""

import os
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Repository root: configurable via REPO_ROOT env var, defaults to fixture_repo/
REPO_ROOT = Path(
    os.environ.get("REPO_ROOT", str(Path(__file__).parent / "fixture_repo"))
).resolve()

MAX_LINES = 200
MAX_BYTES = 8192

# Only these git subcommands are allowed (read-only)
ALLOWED_GIT_COMMANDS = {"log", "show", "diff", "rev-parse", "ls-files"}

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def safe_path(rel: str) -> Path:
    """Resolve a relative path within the repo root, rejecting traversal.

    Raises ValueError if the resolved path escapes the repository root.
    This is the path jail that prevents ../../etc/passwd attacks.
    """
    # Normalize and resolve
    p = (REPO_ROOT / rel).resolve()
    if not p.is_relative_to(REPO_ROOT):
        raise ValueError(
            f"Path '{rel}' escapes the repository root. "
            "Only paths within the repository are allowed."
        )
    return p


def truncate_output(text: str, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> str:
    """Truncate output to stay within limits. Appends a truncation notice."""
    lines = text.splitlines(keepends=True)
    if len(lines) > max_lines:
        text = "".join(lines[:max_lines])
        text += f"\n... [TRUNCATED: showing {max_lines}/{len(lines)} lines. Narrow your query.]\n"
    if len(text.encode("utf-8", errors="replace")) > max_bytes:
        text = text[:max_bytes]
        text += "\n... [TRUNCATED: output exceeded 8KB. Narrow your query.]\n"
    return text


def run_subprocess(args: list[str], cwd: Path | None = None, timeout: int = 15) -> str:
    """Run a subprocess safely. Never uses shell=True."""
    result = subprocess.run(
        args,
        cwd=cwd or REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        return f"Error (exit {result.returncode}): {result.stderr.strip()}"
    return result.stdout


# ---------------------------------------------------------------------------
# Tool implementations (plain Python functions)
# ---------------------------------------------------------------------------


def _list_files() -> str:
    """List all tracked files in the repository."""
    output = run_subprocess(["git", "ls-files"], cwd=REPO_ROOT)
    return truncate_output(output)


def _search_code(pattern: str, glob: str | None = None) -> str:
    """Search for a regex pattern across tracked files using git grep."""
    if not pattern or not pattern.strip():
        return "Error: pattern must not be empty."
    
    # Use git grep instead of rg for better cross-platform reliability
    # git grep -n <pattern>
    args = ["git", "grep", "-n", "-I", pattern]
    if glob:
        args.extend(["--", glob])
        
    output = run_subprocess(args, cwd=REPO_ROOT, timeout=10)
    
    # git grep exits with 1 if no matches found
    if output.startswith("Error (exit 1):"):
        return "No matches found."
        
    # Limit lines manually since git grep doesn't have --max-count
    lines = output.splitlines()
    if len(lines) > 50:
        output = "\n".join(lines[:50]) + f"\n... [TRUNCATED: showing 50/{len(lines)} matches]"
        
    return truncate_output(output)


def _read_file(path: str) -> str:
    """Read a file from the repository with path-traversal protection."""
    try:
        resolved = safe_path(path)
    except ValueError as e:
        return f"Error: {e}"

    if not resolved.exists():
        return f"Error: file '{path}' not found. Try list_files to see available files."
    if not resolved.is_file():
        return f"Error: '{path}' is not a file."

    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"

    return truncate_output(content)


def _git_log(max_count: int = 20, path: str | None = None) -> str:
    """Show git commit history."""
    args = ["git", "log", f"--max-count={max_count}",
            "--format=%H %ai %s", "--no-merges"]
    if path:
        try:
            safe_path(path)  # validate path doesn't escape
        except ValueError as e:
            return f"Error: {e}"
        args.extend(["--", path])
    output = run_subprocess(args, cwd=REPO_ROOT)
    return truncate_output(output)


def _git_show(commit: str) -> str:
    """Show the diff for a specific commit."""
    # Validate commit-ish: only allow hex hashes and simple refs
    if not all(c in "0123456789abcdefABCDEF" for c in commit) and not commit.replace("-", "").replace("_", "").replace("/", "").isalnum():
        return "Error: invalid commit reference. Use a commit hash from git_log."
    args = ["git", "show", "--stat", "--patch", commit]
    output = run_subprocess(args, cwd=REPO_ROOT)
    return truncate_output(output)


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("RepoSleuth", instructions="Read-only repository investigation tools")


@mcp.tool()
def list_files() -> str:
    """List all tracked files in the repository. Use this first to understand
    the project structure before reading specific files."""
    return _list_files()


@mcp.tool()
def search_code(pattern: str, glob: str = "") -> str:
    """Search for a regex pattern across all tracked files. Returns matching
    lines as path:line_number:content. Use this to locate relevant code
    before reading full files. Results are limited to 50 matches.

    Args:
        pattern: A regex pattern to search for (e.g. 'JWT_SECRET', 'def verify_').
        glob: Optional file glob filter (e.g. '*.py'). Empty string means all files.
    """
    return _search_code(pattern, glob if glob else None)


@mcp.tool()
def read_file(path: str) -> str:
    """Read the contents of a file in the repository. Path must be relative to
    the repository root (e.g. 'app/config.py'). Output is limited to 200 lines.
    Use search_code first to find the relevant file, then read_file to see its
    full content.

    Args:
        path: Relative path to the file from the repository root.
    """
    return _read_file(path)


@mcp.tool()
def git_log(max_count: int = 20, path: str = "") -> str:
    """Show git commit history, newest first. Each line shows:
    commit_hash date commit_message. Use this to find when changes were made.
    Then use git_show to inspect specific commits.

    Args:
        max_count: Maximum number of commits to show (default 20).
        path: Optional file path to filter commits (e.g. 'app/config.py').
    """
    return _git_log(max_count, path if path else None)


@mcp.tool()
def git_show(commit: str) -> str:
    """Show the full diff for a specific commit. Includes file stats and
    patch content. Use a commit hash from git_log output.

    Args:
        commit: The commit hash to inspect (from git_log output).
    """
    return _git_show(commit)


if __name__ == "__main__":
    mcp.run(transport="stdio")
