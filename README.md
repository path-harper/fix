# Fix GitHub App

This GitHub App automatically fixes commit messages by removing "add " prefixes when pushes are made to configured repositories. It listens for push events via webhooks, clones the repository, rewrites the commit history to clean up messages, and force pushes the changes back, ensuring consistent and clean commit logs without manual intervention.

To set up the app, start by creating a new GitHub App in your GitHub Settings under Developer settings > GitHub Apps. Click "New GitHub App" and fill in the details: set the GitHub App name to "Fix", provide a homepage URL like https://github.com/yourusername/fix-app (or any placeholder URL), enter the webhook URL pointing to your deployed app's endpoint (e.g., https://your-server.com/webhook), and generate a random webhook secret using a command like `openssl rand -hex 20`. Under permissions, grant Repository contents read and write access, and Repository metadata read-only access. Subscribe to the Push event to trigger the app on commits. After saving, download the private key from the app settings and save it securely to a file, such as `private-key.pem`.

Next, install the app on your desired repository or organization. Then, set the required environment variables: GITHUB_APP_ID with your app's ID, GITHUB_PRIVATE_KEY with the content of the private key file (paste the entire PEM content), and GITHUB_WEBHOOK_SECRET with the webhook secret you generated. Run the app locally with `python github_app.py` for testing, or deploy it to a production server like Render, Heroku, or AWS, updating the webhook URL accordingly to ensure it's publicly accessible.

The app operates by receiving push webhooks from GitHub. Upon a push event, it generates an installation access token, clones the repository using that token for authentication, checks out the affected branch, and executes `git filter-branch` to modify commit messages by removing " add " substrings and leading "add " phrases. Finally, it force pushes the rewritten history back to the branch, skipping actions if the push originated from a bot to prevent infinite loops.

Be warned that this app rewrites git history, which is a destructive operation that can overwrite remote commits and affect collaborators. Always back up your repository before using it, and communicate with your team to avoid conflicts.

The app requires Python 3.8 or later, along with the Flask, PyGitHub, and github-webhook libraries. Install these dependencies using `pip install -r requirements.txt`.

## Deploying to Render

To deploy on Render.com:

1. Push this code to a GitHub repository.
2. Sign up for Render and connect your GitHub account.
3. Create a new Web Service from your repo.
4. Set the following:
   - **Environment**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python github_app.py`
5. Add environment variables in Render's dashboard:
   - `GITHUB_APP_ID`: Your GitHub App ID
   - `GITHUB_PRIVATE_KEY`: The full content of your private key (PEM format)
   - `GITHUB_WEBHOOK_SECRET`: Your webhook secret
6. Deploy and note the service URL (e.g., https://your-app.onrender.com).
7. Update your GitHub App's webhook URL to `https://your-app.onrender.com/webhook`.