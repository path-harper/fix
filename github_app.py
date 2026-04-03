#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Optional

from flask import Flask, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from github import GithubIntegration
from github_webhook import Webhook

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

    # Fix commit messages
    success = fix_commit_messages(repo_url, token, branch)
    if success:
        logger.info(f"Successfully fixed commit messages for {repo_url} on {branch}")
        return jsonify({"status": "Commit messages fixed and pushed"}), 200
    else:
        logger.error(f"Failed to fix commit messages for {repo_url} on {branch}")
        return jsonify({"error": "Failed to fix commit messages"}), 500


@app.route("/")
def index() -> str:
    return "GitHub App for fixing commit messages is running"


if __name__ == "__main__":
    if not all([APP_ID, PRIVATE_KEY, WEBHOOK_SECRET]):
        print(
            "Error: Missing environment variables. Set GITHUB_APP_ID, GITHUB_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET",
        )
        sys.exit(1)
    app.run(host="0.0.0.0", port=5000)
