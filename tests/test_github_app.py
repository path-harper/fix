from unittest.mock import MagicMock, patch

from github_app import app, fix_commit_messages, get_installation_token, run_command


class TestRunCommand:
    @patch("subprocess.run")
    def test_run_command_success(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="success output",
            stderr="error output",
        )
        ret, stdout, stderr = run_command("echo hello")
        assert ret == 0
        assert stdout == "success output"
        assert stderr == "error output"
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_run_command_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="command failed",
        )
        ret, stdout, stderr = run_command("invalid command")
        assert ret == 1
        assert stdout == ""
        assert stderr == "command failed"

    @patch("subprocess.run")
    def test_run_command_exception(self, mock_run):
        mock_run.side_effect = Exception("subprocess error")
        ret, stdout, stderr = run_command("echo hello")
        assert ret == 1
        assert stdout == ""
        assert stderr == "subprocess error"


class TestGetInstallationToken:
    @patch("github_app.GithubIntegration")
    def test_get_installation_token_success(self, mock_integration):
        mock_integration.return_value.get_access_token.return_value.token = "test_token"
        token = get_installation_token(123)
        assert token == "test_token"

    @patch("github_app.GithubIntegration")
    def test_get_installation_token_failure(self, mock_integration):
        mock_integration.return_value.get_access_token.side_effect = Exception(
            "API error",
        )
        token = get_installation_token(123)
        assert token is None


class TestFixCommitMessages:
    @patch("github_app.run_command")
    def test_fix_commit_messages_success(self, mock_run):
        # Mock all commands to succeed
        mock_run.side_effect = [
            (0, "", ""),  # clone
            (0, "", ""),  # checkout
            (0, "", ""),  # filter-branch
            (0, "", ""),  # push
        ]
        success = fix_commit_messages(
            "https://github.com/user/repo.git", "token", "main"
        )
        assert success is True
        assert mock_run.call_count == 4

    @patch("github_app.run_command")
    def test_fix_commit_messages_clone_failure(self, mock_run):
        mock_run.return_value = (1, "", "clone failed")
        success = fix_commit_messages(
            "https://github.com/user/repo.git", "token", "main"
        )
        assert success is False

    @patch("github_app.run_command")
    def test_fix_commit_messages_push_failure(self, mock_run):
        mock_run.side_effect = [
            (0, "", ""),  # clone
            (0, "", ""),  # checkout
            (0, "", ""),  # filter-branch
            (1, "", "push failed"),  # push
        ]
        success = fix_commit_messages(
            "https://github.com/user/repo.git", "token", "main"
        )
        assert success is False


class TestWebhook:
    def test_handle_push_bot_skip(self):
        with app.test_client() as client:
            data = {
                "sender": {"type": "Bot"},
                "repository": {"clone_url": "https://github.com/user/repo.git"},
                "ref": "refs/heads/main",
                "installation": {"id": 123},
            }
            response = client.post("/webhook", json=data)
            assert response.status_code == 200
            assert b"Skipped bot push" in response.data

    @patch("github_app.get_installation_token")
    @patch("github_app.fix_commit_messages")
    def test_handle_push_success(self, mock_fix, mock_token):
        mock_token.return_value = "token"
        mock_fix.return_value = True
        with app.test_client() as client:
            data = {
                "sender": {"type": "User"},
                "repository": {"clone_url": "https://github.com/user/repo.git"},
                "ref": "refs/heads/main",
                "installation": {"id": 123},
            }
            response = client.post("/webhook", json=data)
            assert response.status_code == 200
            assert b"Commit messages fixed" in response.data

    def test_handle_push_missing_data(self):
        with app.test_client() as client:
            data = {}
            response = client.post("/webhook", json=data)
            assert response.status_code == 400
            assert b"Missing required data" in response.data

    @patch("github_app.get_installation_token")
    def test_handle_push_token_failure(self, mock_token):
        mock_token.return_value = None
        with app.test_client() as client:
            data = {
                "sender": {"type": "User"},
                "repository": {"clone_url": "https://github.com/user/repo.git"},
                "ref": "refs/heads/main",
                "installation": {"id": 123},
            }
            response = client.post("/webhook", json=data)
            assert response.status_code == 500
            assert b"Could not get installation token" in response.data

    @patch("github_app.get_installation_token")
    @patch("github_app.fix_commit_messages")
    def test_handle_push_fix_failure(self, mock_fix, mock_token):
        mock_token.return_value = "token"
        mock_fix.return_value = False
        with app.test_client() as client:
            data = {
                "sender": {"type": "User"},
                "repository": {"clone_url": "https://github.com/user/repo.git"},
                "ref": "refs/heads/main",
                "installation": {"id": 123},
            }
            response = client.post("/webhook", json=data)
            assert response.status_code == 500
            assert b"Failed to fix commit messages" in response.data
