#!/usr/bin/env python3

import os
import tempfile
import subprocess
import sys
from flask import Flask, request, jsonify
from github import GithubIntegration
from github_webhook import Webhook

app = Flask(__name__)

# GitHub App configuration
# Replace these with your actual values
APP_ID = os.getenv("GITHUB_APP_ID")  # Set as environment variable
PRIVATE_KEY = os.getenv("GITHUB_PRIVATE_KEY")  # Private key content as string
WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")  # Webhook secret for verification

webhook = Webhook(WEBHOOK_SECRET)


def run_command(cmd, cwd=None, env=None):
    """Run a shell command."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, cwd=cwd, env=env
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def get_installation_token(installation_id):
    """Get an installation access token for the given installation."""
    try:
        integration = GithubIntegration(APP_ID, PRIVATE_KEY)
        token = integration.get_access_token(installation_id)
        return token.token
    except Exception as e:
        print(f"Error getting installation token: {e}")
        return None


def fix_commit_messages(repo_url, token, branch):
    """Clone repo, fix commit messages, and push back."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Clone the repo using token
        clone_url = repo_url.replace("https://", f"https://x-access-token:{token}@")
        ret, _, err = run_command(f"git clone {clone_url} repo", cwd=temp_dir)
        if ret != 0:
            print(f"Failed to clone repo: {err}")
            return False

        repo_path = os.path.join(temp_dir, "repo")

        # Checkout the branch
        ret, _, err = run_command(f"git checkout {branch}", cwd=repo_path)
        if ret != 0:
            print(f"Failed to checkout branch {branch}: {err}")
            return False

        # Run git filter-branch
        filter_cmd = """
commit_msg=$(cat);
commit_msg=$(echo "$commit_msg" | sed "s/ add / /g");
commit_msg=$(echo "$commit_msg" | sed "s/^add //");
echo "$commit_msg"
"""
        cmd = f"git filter-branch -f --msg-filter '{filter_cmd}' {branch}"
        ret, _, err = run_command(cmd, cwd=repo_path)
        if ret != 0:
            print(f"Warning: git filter-branch failed: {err}")
            return False

        # Push force
        ret, _, err = run_command(f"git push --force origin {branch}", cwd=repo_path)
        if ret != 0:
            print(f"Failed to push: {err}")
            return False

        return True


@app.route("/webhook", methods=["POST"])
@webhook.hook("push")
def handle_push():
    """Handle push webhook."""
    data = request.json
    if not data:
        return jsonify({"error": "No data"}), 400

    # Skip if push is from a bot (e.g., this app itself) to prevent loops
    sender = data.get("sender", {})
    if sender.get("type") == "Bot":
        return jsonify({"status": "Skipped bot push"}), 200

    repo = data.get("repository", {})
    repo_url = repo.get("clone_url")
    branch = data.get("ref", "").replace("refs/heads/", "")
    installation_id = data.get("installation", {}).get("id")

    if not all([repo_url, branch, installation_id]):
        return jsonify({"error": "Missing required data"}), 400

    # Get installation token
    token = get_installation_token(installation_id)
    if not token:
        return jsonify({"error": "Could not get installation token"}), 500

    # Fix commit messages
    success = fix_commit_messages(repo_url, token, branch)
    if success:
        return jsonify({"status": "Commit messages fixed and pushed"}), 200
    else:
        return jsonify({"error": "Failed to fix commit messages"}), 500


@app.route("/")
def index():
    return "GitHub App for fixing commit messages is running"


if __name__ == "__main__":
    if not all([APP_ID, PRIVATE_KEY, WEBHOOK_SECRET]):
        print(
            "Error: Missing environment variables. Set GITHUB_APP_ID, GITHUB_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET"
        )
        sys.exit(1)
    app.run(host="0.0.0.0", port=5000)
