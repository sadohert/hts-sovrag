"""The review workflow: one agent, one pass, and a green check that means a review was posted.

The guard step is inline in the workflow so that the action's "identical to the default branch"
validation covers it. These tests pull the step out of the YAML and run it with a fake `gh`.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(".github/workflows/claude-code-review.yml")
REVIEW_STEP = "Run Claude Code Review"
GUARD_STEP = "Verify the review was posted"

# Read the pull request, read the repository, post the review. Nothing that writes elsewhere.
ALLOWED_TOOLS = (
    "mcp__github_inline_comment__create_inline_comment",
    "Bash(gh pr comment:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr view:*)",
    "Read",
    "Grep",
    "Glob",
)

needs_shell_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq"
)


def _workflow() -> dict[str, object]:
    loaded: dict[str, object] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def _steps() -> list[dict[str, object]]:
    steps: list[dict[str, object]] = _workflow()["jobs"]["claude-review"]["steps"]  # type: ignore[index]
    return steps


def _step(name: str) -> dict[str, object]:
    return next(s for s in _steps() if s.get("name") == name)


def _with(name: str) -> dict[str, object]:
    inputs: dict[str, object] = _step(name)["with"]  # type: ignore[assignment]
    return inputs


def _flag(claude_args: str, name: str) -> str:
    """The quoted value that follows a flag in the claude_args text."""
    return claude_args.split(f'{name} "', 1)[1].split('"', 1)[0]


def _run_guard(
    tmp_path: Path, *, execution: list[dict[str, object]] | None, summaries: int, inline: int = 0
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    # Prints one id per post: FAKE_SUMMARIES for the issue-comments endpoint, FAKE_INLINE for
    # the pull-request-comments endpoint. A loop rather than `seq`, because BSD `seq 1 0`
    # counts down and prints two lines.
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *issues/*) n=$FAKE_SUMMARIES ;; *pulls/*) n=$FAKE_INLINE ;; *) exit 0 ;;\n'
        "esac\n"
        'i=0; while [ "$i" -lt "$n" ]; do echo "$i"; i=$((i + 1)); done\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    execution_file = ""
    if execution is not None:
        path = tmp_path / "execution.json"
        path.write_text(json.dumps(execution), encoding="utf-8")
        execution_file = str(path)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "REPO": "owner/repo",
        "PR": "7",
        "EXECUTION_FILE": execution_file,
        "FAKE_SUMMARIES": str(summaries),
        "FAKE_INLINE": str(inline),
    }
    script = str(_step(GUARD_STEP)["run"])
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _execution(turns: int, denied: list[str]) -> list[dict[str, object]]:
    return [
        {"type": "system", "subtype": "init"},
        {
            "type": "result",
            "num_turns": turns,
            "total_cost_usd": 0.06,
            "permission_denials": [
                {"tool_name": "Bash", "tool_input": {"command": command}} for command in denied
            ],
        },
    ]


# The review step: one agent with an inline prompt, no plugin, read-only tools plus posting.


def test_the_review_is_a_single_agent_with_an_inline_prompt_and_no_plugin() -> None:
    inputs = _with(REVIEW_STEP)
    assert "plugins" not in inputs and "plugin_marketplaces" not in inputs
    assert "track_progress" not in inputs, "a tracking comment would satisfy the guard by itself"
    assert "show_full_output" not in inputs
    prompt = str(inputs["prompt"])
    assert "CLAUDE.md" in prompt and "Invariants" in prompt
    assert "gh pr diff" in prompt


def test_allowed_tools_are_exactly_read_the_pull_request_read_the_repo_and_post() -> None:
    allowed = _flag(str(_with(REVIEW_STEP)["claude_args"]), "--allowedTools")
    assert tuple(allowed.split(",")) == ALLOWED_TOOLS


def test_the_prompt_makes_a_finished_review_always_post_a_summary_the_guard_can_find() -> None:
    prompt = str(_with(REVIEW_STEP)["prompt"])
    assert '"## Code review"' in prompt
    assert "gh pr comment" in prompt
    assert "No issues found" in prompt


def test_the_model_is_named_so_the_cost_does_not_follow_the_action_default() -> None:
    claude_args = str(_with(REVIEW_STEP)["claude_args"])
    assert "--model claude-sonnet-5" in claude_args


def test_the_action_is_pinned_to_a_commit() -> None:
    uses = str(_step(REVIEW_STEP)["uses"])
    ref = uses.split("@", 1)[1]
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), uses


def test_a_new_push_cancels_the_review_of_the_previous_commit() -> None:
    concurrency: dict[str, object] = _workflow()["concurrency"]  # type: ignore[assignment]
    assert concurrency["cancel-in-progress"] is True
    assert "pull_request.number" in str(concurrency["group"])


def test_guard_runs_after_the_review_and_reads_its_execution_file() -> None:
    names = [s.get("name") for s in _steps()]
    assert names.index(GUARD_STEP) > names.index(REVIEW_STEP)
    env: dict[str, object] = _step(GUARD_STEP)["env"]  # type: ignore[assignment]
    assert env["EXECUTION_FILE"] == "${{ steps.claude-review.outputs.execution_file }}"


# The guard step, run for real with a fake gh.


@needs_shell_tools
def test_a_skipped_action_warns_that_nothing_was_reviewed_but_passes(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, execution=None, summaries=0)
    assert result.returncode == 0, result.stderr
    assert "::warning" in result.stdout
    assert "not reviewed" in result.stdout.lower()


@needs_shell_tools
def test_a_review_that_posted_no_summary_fails_and_says_why(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, execution=_execution(3, ["gh pr view 7 --comments"]), summaries=0)
    assert result.returncode == 1
    assert "::error" in result.stdout
    assert "turns=3" in result.stdout
    assert "denials=1" in result.stdout
    assert "denied: Bash gh pr view 7 --comments" in result.stdout


@needs_shell_tools
def test_inline_comments_alone_do_not_satisfy_the_guard(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, execution=_execution(9, []), summaries=0, inline=2)
    assert result.returncode == 1
    assert "inline comments=2" in result.stdout


@needs_shell_tools
def test_a_review_that_posted_its_summary_passes(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, execution=_execution(14, []), summaries=1, inline=3)
    assert result.returncode == 0, result.stderr
    assert "::error" not in result.stdout
    assert "turns=14" in result.stdout
    assert "summaries=1 inline=3" in result.stdout
