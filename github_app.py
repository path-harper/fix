#!/usr/bin/env python3

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

import redis
from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from github import GithubIntegration
from github_webhook import Webhook

REDIS_URL = os.getenv("REDIS_URL")
STATS_KEY = "fix:stats"


def load_stats() -> Union[dict[str, Any], Any]:
    """Load stats from Redis or file."""
    if REDIS_URL:
        try:
            r = redis.from_url(REDIS_URL)
            data = r.get(STATS_KEY)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.warning(f"Failed to load stats from Redis: {e}")
    try:
        return json.loads(Path("stats.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"total_pushes": 0, "repos": {}}


def save_stats(stats: dict[str, Any]) -> None:
    """Save stats to Redis and file."""
    data = json.dumps(stats, indent=2)
    if REDIS_URL:
        try:
            r = redis.from_url(REDIS_URL)
            r.set(STATS_KEY, data)
        except Exception as e:
            logger.warning(f"Failed to save stats to Redis: {e}")
    Path("stats.json").write_text(data)


def increment_stats(repo_name: str, *, skipped: bool = False) -> None:
    """Increment push count for a repository."""
    stats = load_stats()
    stats["total_pushes"] = stats.get("total_pushes", 0) + 1
    if repo_name not in stats["repos"]:
        stats["repos"][repo_name] = {"pushes": 0, "skipped": 0}
    stats["repos"][repo_name]["pushes"] = stats["repos"][repo_name].get("pushes", 0) + 1
    if skipped:
        stats["repos"][repo_name]["skipped"] = (
            stats["repos"][repo_name].get("skipped", 0) + 1
        )
    save_stats(stats)


app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up rate limiting
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://",
)

# GitHub App configuration
# Replace these with your actual values
APP_ID = os.getenv("GITHUB_APP_ID")  # Set as environment variable
PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY")  # Private key content as string
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")  # Webhook secret for verification

webhook = Webhook(app, secret=WEBHOOK_SECRET)


def run_command(
    cmd: Union[str, list[str]],
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> tuple[int, str, str]:
    """Run a command and capture output."""
    try:
        use_shell = isinstance(cmd, str)
        result = subprocess.run(
            cmd,
            shell=use_shell,  # noqa: S603
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def get_installation_token(installation_id: int) -> Optional[str]:
    """Get an installation access token for the given installation."""
    try:
        integration = GithubIntegration(APP_ID, PRIVATE_KEY)
        auth = integration.get_access_token(installation_id)
        return str(auth.token)
    except Exception as e:
        logger.error(f"Error getting installation token for {installation_id}: {e}")
        return None


def normalize_commit_message(message: str) -> str:
    cleaned = message.replace(" add ", " ").replace(" Add ", " ")
    return re.sub(r"(?m)^(add|Add) ", "", cleaned)


def git(repo_path: str, args: list[str]) -> tuple[int, str, str]:
    return run_command(["git", *args], cwd=repo_path)


def get_merge_base(
    repo_path: str,
    *,
    branch: str,
    default_branch: str,
    repo_url: str,
) -> Optional[str]:
    ret, _, err = git(repo_path, ["fetch", "origin", default_branch])
    if ret != 0:
        logger.warning(f"Failed to fetch origin/{default_branch} for {repo_url}: {err}")
        return None

    ret, base_sha, err = git(
        repo_path,
        ["merge-base", "HEAD", f"origin/{default_branch}"],
    )
    if ret != 0 or not base_sha:
        logger.warning(
            f"Failed to find merge-base for {repo_url} ({branch} vs {default_branch}): {err}",
        )
        return None
    return base_sha


def list_commits(
    repo_path: str,
    *,
    base_sha: str,
    branch: str,
    repo_url: str,
) -> Optional[list[str]]:
    ret, commit_list, err = git(
        repo_path,
        ["rev-list", "--reverse", f"{base_sha}..HEAD"],
    )
    if ret != 0:
        logger.warning(f"Failed to list commits for {repo_url} on {branch}: {err}")
        return None

    return [c for c in commit_list.splitlines() if c.strip()]


def commits_need_rewrite(
    repo_path: str,
    commits: list[str],
    repo_url: str,
) -> Optional[bool]:
    for sha in commits:
        ret, msg, err = git(repo_path, ["log", "-1", "--format=%B", sha])
        if ret != 0:
            logger.warning(f"Failed to read commit message {sha} for {repo_url}: {err}")
            return None
        if normalize_commit_message(msg) != msg:
            return True
    return False


def write_msg_filter_script(repo_path: str) -> Path:
    filter_script_path = Path(repo_path) / ".git" / "msg_filter.sh"
    filter_script_path.parent.mkdir(parents=True, exist_ok=True)
    filter_script_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
python3 - <<'PY'
import re
import sys

msg = sys.stdin.read()
msg = msg.replace(" add ", " ").replace(" Add ", " ")
msg = re.sub(r"(?m)^(add|Add) ", "", msg)
sys.stdout.write(msg)
PY
""",
    )
    filter_script_path.chmod(0o700)
    return filter_script_path


def rewrite_and_push(
    repo_path: str,
    *,
    base_sha: str,
    branch: str,
    repo_url: str,
) -> bool:
    filter_script_path = write_msg_filter_script(repo_path)

    ret, _, err = git(
        repo_path,
        [
            "filter-branch",
            "-f",
            "--msg-filter",
            str(filter_script_path),
            "--",
            f"{base_sha}..{branch}",
        ],
    )
    if ret != 0:
        logger.warning(f"git filter-branch failed for {repo_url} on {branch}: {err}")
        return False

    ret, _, err = git(repo_path, ["push", "--force-with-lease", "origin", branch])
    if ret != 0:
        logger.error(f"Failed to push changes to {repo_url} on {branch}: {err}")
        return False

    return True


def fix_commit_messages(
    repo_url: str,
    token: str,
    branch: str,
    *,
    default_branch: str,
) -> bool:
    """Clone repo, fix commit messages, and push back."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Clone the repo using token
        clone_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
        ret, _, err = run_command(["git", "clone", clone_url, "repo"], cwd=temp_dir)
        if ret != 0:
            logger.error(f"Failed to clone repo {repo_url}: {err}")
            return False

        repo_path = os.path.join(temp_dir, "repo")

        # Checkout the branch
        ret, _, err = git(repo_path, ["checkout", branch])
        if ret != 0:
            logger.error(f"Failed to checkout branch {branch} in {repo_url}: {err}")
            return False

        if branch == default_branch:
            logger.info(
                f"Skipping commit-message rewriting on default branch {default_branch}",
            )
            return True

        base_sha = get_merge_base(
            repo_path,
            branch=branch,
            default_branch=default_branch,
            repo_url=repo_url,
        )
        if not base_sha:
            return False

        commits = list_commits(
            repo_path,
            base_sha=base_sha,
            branch=branch,
            repo_url=repo_url,
        )
        if commits is None:
            return False

        if not commits:
            return True

        needs_rewrite = commits_need_rewrite(repo_path, commits, repo_url)
        if needs_rewrite is None:
            return False

        if not needs_rewrite:
            logger.info(f"No commit messages to rewrite for {repo_url} on {branch}")
            return True

        return rewrite_and_push(
            repo_path,
            base_sha=base_sha,
            branch=branch,
            repo_url=repo_url,
        )


@app.route("/webhook", methods=["POST"])
@limiter.limit("10 per minute")
def handle_push() -> tuple[Any, int]:
    """Handle push webhook."""
    data = request.json
    if data is None:
        logger.warning("Received push webhook with no data")
        return jsonify({"error": "No data"}), 400

    # Skip if push is from a bot (e.g., this app itself) to prevent loops
    sender = data.get("sender", {})
    if sender.get("type") == "Bot":
        repo = data.get("repository", {})
        repo_name = repo.get("full_name", "")
        if repo_name:
            increment_stats(repo_name, skipped=True)
        logger.info("Skipped push from bot to prevent loops")
        return jsonify({"status": "Skipped bot push"}), 200

    repo = data.get("repository", {})
    repo_url = repo.get("clone_url")
    default_branch = repo.get("default_branch", "main")
    branch = data.get("ref", "").replace("refs/heads/", "")
    installation_id = data.get("installation", {}).get("id")

    if not all([repo_url, branch, installation_id]):
        logger.error(
            "Push webhook missing required data: repo_url, branch, or installation_id",
        )
        return jsonify({"error": "Missing required data"}), 400

    # Sanitize repo_url
    if not re.match(r"^https://github\.com/[\w.-]+/[\w.-]+\.git$", repo_url):
        logger.error(f"Invalid repo_url: {repo_url}")
        return jsonify({"error": "Invalid repository URL"}), 400

    # Get installation token
    token = get_installation_token(installation_id)
    if not token:
        logger.error(f"Could not get installation token for {installation_id}")
        return jsonify({"error": "Could not get installation token"}), 500

    # Extract repo name for stats
    repo_name = repo.get("full_name", "")

    # Increment stats for all pushes (including bot pushes)
    if repo_name:
        increment_stats(repo_name)

    # Fix commit messages
    success = fix_commit_messages(
        repo_url,
        token,
        branch,
        default_branch=default_branch,
    )
    if success:
        if repo_name:
            increment_stats(repo_name)
        logger.info(f"Successfully fixed commit messages for {repo_url} on {branch}")
        return jsonify({"status": "Commit messages fixed and pushed"}), 200
    else:
        logger.error(f"Failed to fix commit messages for {repo_url} on {branch}")
        return jsonify({"error": "Failed to fix commit messages"}), 500


@app.route("/")
def index() -> Any:
    """Serve the index.html page."""
    try:
        from pathlib import Path

        html_content = Path("docs/index.html").read_text()
        return html_content, 200, {"Content-Type": "text/html"}
    except Exception:
        return "GitHub App for fixing commit messages is running"


@app.route("/images/<path:filename>")
def serve_image(filename: str) -> Any:
    """Serve images from docs/images directory."""
    try:
        from pathlib import Path

        img_path = Path(f"docs/images/{filename}")
        if img_path.exists():
            content = img_path.read_bytes()
            ext = filename.split(".")[-1]
            mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "svg": "image/svg+xml",
            }.get(ext, "image/png")
            return content, 200, {"Content-Type": mime}
    except Exception as e:
        logger.warning(f"Failed to serve image {filename}: {e}")
    return "Not found", 404


@app.route("/app-screenshot.png")
def serve_screenshot() -> Any:
    """Serve the screenshot image."""
    try:
        from pathlib import Path

        img_path = Path("docs/app-screenshot.png")
        if img_path.exists():
            content = img_path.read_bytes()
            return content, 200, {"Content-Type": "image/png"}
    except Exception as e:
        logger.warning(f"Failed to serve screenshot: {e}")
    return "Not found", 404


@app.route("/stats")
def stats() -> tuple[Any, int]:
    """Return push statistics."""
    data = load_stats()
    return jsonify(data), 200


if __name__ == "__main__":
    if not all([APP_ID, PRIVATE_KEY, WEBHOOK_SECRET]):
        print(
            "Error: Missing environment variables. Set GITHUB_APP_ID, GITHUB_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET",
        )
        sys.exit(1)
    app.run(host="0.0.0.0", port=5000)
