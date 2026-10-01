"""Tests for the repository investigation tools.

Tests cover:
  - Normal tool behavior
  - Invalid input behavior
  - Path traversal rejection
  - Read-only behavior
  - Output truncation

These tests must run AFTER make_fixture.py has generated the fixture repository.
"""

import os
import sys
from pathlib import Path

import pytest

# Add project root to path so we can import server module
sys.path.insert(0, str(Path(__file__).parent.parent))

# We need to set REPO_ROOT before importing server functions
FIXTURE_DIR = Path(__file__).parent.parent / "fixture_repo"


@pytest.fixture(autouse=True)
def set_repo_root(monkeypatch):
    """Set the REPO_ROOT for all tests to the fixture directory."""
    monkeypatch.setattr("server.REPO_ROOT", FIXTURE_DIR.resolve())


@pytest.fixture(autouse=True)
def check_fixture():
    """Skip tests if fixture repo hasn't been generated."""
    if not FIXTURE_DIR.exists():
        pytest.skip("Fixture repository not found. Run: python make_fixture.py")


# ---------------------------------------------------------------------------
# list_files tests
# ---------------------------------------------------------------------------

class TestListFiles:
    def test_returns_tracked_files(self):
        from server import _list_files
        result = _list_files()
        assert "app/config.py" in result
        assert "app/auth.py" in result
        assert "app/main.py" in result

    def test_includes_all_expected_files(self):
        from server import _list_files
        result = _list_files()
        assert "README.md" in result
        assert "requirements.txt" in result
        assert "app/__init__.py" in result


# ---------------------------------------------------------------------------
# search_code tests
# ---------------------------------------------------------------------------

class TestSearchCode:
    def test_finds_pattern(self):
        from server import _search_code
        result = _search_code("JWT_ALGORITHM")
        assert "JWT_ALGORITHM" in result

    def test_empty_pattern_error(self):
        from server import _search_code
        result = _search_code("")
        assert "Error" in result or "empty" in result.lower()

    def test_no_results(self):
        from server import _search_code
        result = _search_code("THIS_PATTERN_DOES_NOT_EXIST_ANYWHERE_12345")
        # ripgrep returns exit code 1 for no matches
        assert "THIS_PATTERN_DOES_NOT_EXIST_ANYWHERE_12345" not in result

    def test_glob_filter(self):
        from server import _search_code
        result = _search_code("import", "*.py")
        assert "import" in result


# ---------------------------------------------------------------------------
# read_file tests
# ---------------------------------------------------------------------------

class TestReadFile:
    def test_reads_existing_file(self):
        from server import _read_file
        result = _read_file("app/config.py")
        assert "configuration" in result.lower() or "config" in result.lower()

    def test_file_not_found(self):
        from server import _read_file
        result = _read_file("nonexistent.py")
        assert "Error" in result
        assert "not found" in result.lower()

    def test_directory_not_a_file(self):
        from server import _read_file
        result = _read_file("app")
        assert "Error" in result

    def test_path_traversal_rejected(self):
        """Critical security test: ../../etc/passwd must be rejected."""
        from server import _read_file
        result = _read_file("../../etc/passwd")
        assert "Error" in result
        assert "escapes" in result.lower() or "repository root" in result.lower()

    def test_path_traversal_windows_style(self):
        """Test Windows-style traversal."""
        from server import _read_file
        result = _read_file("..\\..\\etc\\passwd")
        assert "Error" in result

    def test_path_traversal_encoded(self):
        """Test traversal with hidden tricks."""
        from server import _read_file
        result = _read_file("app/../../etc/passwd")
        assert "Error" in result


# ---------------------------------------------------------------------------
# safe_path tests
# ---------------------------------------------------------------------------

class TestSafePath:
    def test_valid_path(self):
        from server import safe_path
        p = safe_path("app/config.py")
        assert p.name == "config.py"

    def test_traversal_raises(self):
        from server import safe_path
        with pytest.raises(ValueError, match="escapes"):
            safe_path("../../etc/passwd")

    def test_traversal_via_nested_raises(self):
        from server import safe_path
        with pytest.raises(ValueError, match="escapes"):
            safe_path("app/../../../../../../etc/passwd")

    def test_simple_relative_ok(self):
        from server import safe_path
        p = safe_path("README.md")
        assert "README.md" in str(p)


# ---------------------------------------------------------------------------
# git_log tests
# ---------------------------------------------------------------------------

class TestGitLog:
    def test_returns_commits(self):
        from server import _git_log
        result = _git_log()
        assert "Initial project structure" in result

    def test_max_count(self):
        from server import _git_log
        result = _git_log(max_count=3)
        lines = [l for l in result.strip().splitlines() if l.strip()]
        assert len(lines) <= 3

    def test_filter_by_path(self):
        from server import _git_log
        result = _git_log(path="app/config.py")
        assert "config" in result.lower() or "naming" in result.lower()


# ---------------------------------------------------------------------------
# git_show tests
# ---------------------------------------------------------------------------

class TestGitShow:
    def test_shows_commit(self):
        from server import _git_log, _git_show
        # Get the first commit hash
        log = _git_log(max_count=1)
        commit_hash = log.split()[0]
        result = _git_show(commit_hash)
        assert "diff" in result.lower() or "commit" in result.lower() or len(result) > 0

    def test_invalid_commit(self):
        from server import _git_show
        result = _git_show("0000000000000000000000000000000000000000")
        # Git will return an error for a nonexistent commit
        assert "Error" in result or "fatal" in result.lower() or "bad" in result.lower()


# ---------------------------------------------------------------------------
# Read-only behavior tests
# ---------------------------------------------------------------------------

class TestReadOnly:
    def test_no_write_operations_exposed(self):
        """Verify that no write git operations are exposed."""
        from server import ALLOWED_GIT_COMMANDS
        write_commands = {"push", "commit", "reset", "checkout", "merge", "rebase", "rm", "mv"}
        assert not ALLOWED_GIT_COMMANDS.intersection(write_commands), \
            "Write operations must not be in the allowed commands"


# ---------------------------------------------------------------------------
# Output truncation tests
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_truncate_long_output(self):
        from server import truncate_output
        long_text = "line\n" * 500
        result = truncate_output(long_text, max_lines=10)
        assert "TRUNCATED" in result
        assert result.count("\n") < 500

    def test_short_output_unchanged(self):
        from server import truncate_output
        short_text = "hello\nworld\n"
        result = truncate_output(short_text)
        assert result == short_text
