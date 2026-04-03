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
    cmd: str,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
) -> tuple[int, str, str]:
    """Run a shell command."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
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


def fix_commit_messages(repo_url: str, token: str, branch: str) -> bool:
    """Clone repo, fix commit messages, and push back."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Clone the repo using token
        clone_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
        ret, _, err = run_command(f"git clone {clone_url} repo", cwd=temp_dir)
        if ret != 0:
            logger.error(f"Failed to clone repo {repo_url}: {err}")
            return False

        repo_path = os.path.join(temp_dir, "repo")

        # Checkout the branch
        ret, _, err = run_command(f"git checkout {branch}", cwd=repo_path)
        if ret != 0:
            logger.error(f"Failed to checkout branch {branch} in {repo_url}: {err}")
            return False

        # Run git filter-branch
        filter_cmd = """
commit_msg=$(cat);
commit_msg=$(echo "$commit_msg" | sed "s/ add / /g");
commit_msg=$(echo "$commit_msg" | sed "s/ Add / /g");
commit_msg=$(echo "$commit_msg" | sed "s/^add //");
commit_msg=$(echo "$commit_msg" | sed "s/^Add //");
echo "$commit_msg"
"""
        cmd = f"git filter-branch -f --msg-filter '{filter_cmd}' {branch}"
        ret, _, err = run_command(cmd, cwd=repo_path)
        if ret != 0:
            logger.warning(
                f"git filter-branch failed for {repo_url} on {branch}: {err}",
            )
            return False

        # Push force
        ret, _, err = run_command(f"git push --force origin {branch}", cwd=repo_path)
        if ret != 0:
            logger.error(f"Failed to push changes to {repo_url} on {branch}: {err}")
            return False

        return True


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
    success = fix_commit_messages(repo_url, token, branch)
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
