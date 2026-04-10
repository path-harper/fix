# Changelog

Since releasing `v1.0.0`, we've significantly improved the app with stats tracking that shows total pushes and per-repo counts including skipped bot pushes at the `/stats` endpoint, added an improved documentation page with the stats URL, new logo icon, and screenshot example, and created a ping workflow that runs every 5 minutes to keep the Render service from suspending on the free tier. We also fixed a critical security issue by removing an exposed private key from git history using `git filter-repo`, properly set up the `test-repo` as a GitHub submodule with `.gitmodules` configuration, added type annotations to tests for better code quality, and updated the stats to track both user pushes and bot pushes separately. The app has gone through three releases: `v1.0.0` was the initial release, `v1.1.0` added stats tracking, and `v1.2.0` improved stats to include bot push tracking.

Added `REDIS_URL` environment variable support in `v1.3.0` to persist stats across service restarts using Render Key Value, updated the app to serve `docs/index.html` directly at the root `/` endpoint with endpoints `/images/logo.png` and `/app-screenshot.png` to serve images, and simplified the install button text to be more concise.

## v1.5.0 (2026-04-10)

- Reduce PR branch history rewriting by only rewriting commit messages when a change is needed, and only for commits unique to the pushed branch (relative to the repository default branch).
- Skip rewriting the repository default branch entirely.
- Push rewritten history using `--force-with-lease` instead of `--force`.
- Update docs and webpage copy to reflect the safer behavior.
- Untrack accidentally committed `__pycache__/*.pyc` files.
- Fix empty commit message issue by using Python directly in filter script instead of bash-wrapped heredoc.

## v1.4.0 (2026-04-09)

- Only rewrite new commits (relative to the repository default branch).
- Fix tests/mocks around the commit-rewrite behavior.
- Stop tracking Python `__pycache__/*.pyc` artifacts.
- Update changelog for the release.
