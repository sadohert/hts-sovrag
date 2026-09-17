"""The review workflow: a green check must mean a review landed on the pull request.

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
GUARD_STEP = "Verify the review was posted"

# From the code-review plugin's own `allowed-tools`. Its first step is a subagent that runs
# `gh pr view`; when that is denied the reviewer stops and the job still reports success.
PLUGIN_TOOLS = (
    "mcp__github_inline_comment__create_inline_comment",
    "Bash(gh issue view:*)",
    "Bash(gh search:*)",
    "Bash(gh issue list:*)",
    "Bash(gh pr comment:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr list:*)",
)

# The prompt is a plugin command, which the reviewer loads through the Skill tool. A skill's
# own `allowed-tools` are granted only during the turn that invokes it, so when the Skill call
# itself was denied the grant never happened and the reviewer improvised, then gave up.
SKILL_RULES = (
    "Skill(code-review:code-review)",
    "Skill(code-review:code-review *)",
)

needs_shell_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq"
)


def _steps() -> list[dict[str, object]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, object]] = workflow["jobs"]["claude-review"]["steps"]
    return steps


def _step(name: str) -> dict[str, object]:
    return next(s for s in _steps() if s.get("name") == name)


def _run_guard(
    tmp_path: Path, *, execution: list[dict[str, object]] | None, posted: int
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gh = bin_dir / "gh"
    # Prints one id per claude[bot] post, for the issue-comments endpoint only. A loop rather
    # than `seq`, because BSD `seq 1 0` counts down and prints two lines.
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in *issues/*) ;; *) exit 0 ;; esac\n'
        'i=0; while [ "$i" -lt "$FAKE_POSTS" ]; do echo "$i"; i=$((i + 1)); done\n',
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
        "FAKE_POSTS": str(posted),
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


def test_allowed_tools_cover_everything_the_review_plugin_needs() -> None:
    claude_args = str(_step("Run Claude Code Review")["with"]["claude_args"])  # type: ignore[index]
    for tool in PLUGIN_TOOLS:
        assert tool in claude_args, f"{tool} missing from --allowedTools"


def test_allowed_tools_let_the_reviewer_load_the_review_skill() -> None:
    claude_args = str(_step("Run Claude Code Review")["with"]["claude_args"])  # type: ignore[index]
    for rule in SKILL_RULES:
        assert rule in claude_args, f"{rule} missing from --allowedTools"


def test_guard_runs_after_the_review_and_reads_its_execution_file() -> None:
    names = [s.get("name") for s in _steps()]
    assert names.index(GUARD_STEP) > names.index("Run Claude Code Review")
    env = _step(GUARD_STEP)["env"]
    assert env["EXECUTION_FILE"] == "${{ steps.claude-review.outputs.execution_file }}"  # type: ignore[index]


@needs_shell_tools
def test_a_skipped_action_warns_that_nothing_was_reviewed_but_passes(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, execution=None, posted=0)
    assert result.returncode == 0, result.stderr
    assert "::warning" in result.stdout
    assert "not reviewed" in result.stdout.lower()


@needs_shell_tools
def test_a_review_that_posted_nothing_fails_and_says_why(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, execution=_execution(3, ["gh pr view 7 --comments"]), posted=0)
    assert result.returncode == 1
    assert "::error" in result.stdout
    assert "turns=3" in result.stdout
    assert "denials=1" in result.stdout
    assert "denied: Bash gh pr view 7 --comments" in result.stdout


@needs_shell_tools
def test_a_review_that_posted_passes(tmp_path: Path) -> None:
    result = _run_guard(tmp_path, execution=_execution(14, []), posted=1)
    assert result.returncode == 0, result.stderr
    assert "::error" not in result.stdout
    assert "turns=14" in result.stdout
