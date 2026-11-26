#!/usr/bin/env python3
"""Refresh the monitored section of README.md from the GitHub GraphQL API.

Only the Python standard library is required. Locally, a `gh auth login` session
is used when GITHUB_TOKEN / GH_TOKEN is not set. In Actions, GITHUB_TOKEN is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

API_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com"
START_MARKER = "<!-- START MONITOR -->"
END_MARKER = "<!-- END MONITOR -->"
ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
DATA_PATH = ROOT / "data" / "projects.json"
LOGIN = os.environ.get("GITHUB_USER", "taylorivanoff")

QUERY = """
query Monitor($login: String!, $after: String) {
  user(login: $login) {
    repositories(
      first: 100
      after: $after
      ownerAffiliations: [OWNER]
      privacy: PUBLIC
      orderBy: {field: UPDATED_AT, direction: DESC}
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        name
        description
        url
        homepageUrl
        isArchived
        isFork
        updatedAt
        stargazerCount
        primaryLanguage {
          name
        }
        issues(states: OPEN) {
          totalCount
        }
        refs(refPrefix: "refs/tags/", first: 10, orderBy: {field: TAG_COMMIT_DATE, direction: DESC}) {
          nodes {
            name
          }
        }
      }
    }
  }
}
"""


def get_token() -> str:
    """Prefer an env token; fall back to an authenticated GitHub CLI session."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and token.strip():
        return token.strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(
            "No GitHub token found. Set GITHUB_TOKEN or run 'gh auth login'."
        ) from exc
    token = result.stdout.strip()
    if not token:
        raise SystemExit("The GitHub CLI returned an empty token.")
    return token


def auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "projects-monitor",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def request_json(request: urllib.request.Request, allow_404: bool = False):
    """Send a request with basic retry handling for transient failures."""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if allow_404 and exc.code == 404:
                return None
            detail = exc.read().decode("utf-8", "replace")
            if exc.code in {429, 500, 502, 503, 504} and attempt < 2:
                time.sleep(2**attempt)
                continue
            raise SystemExit(f"GitHub API request failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            if attempt < 2:
                time.sleep(2**attempt)
                continue
            raise SystemExit(f"Could not reach the GitHub API: {exc}") from exc
    raise SystemExit("GitHub API request failed after retries.")


def graphql(query: str, variables: dict, token: str) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers=auth_headers(token),
        method="POST",
    )
    payload = request_json(request)
    errors = payload.get("errors")
    if errors:
        raise SystemExit("GitHub GraphQL errors: " + json.dumps(errors))
    return payload["data"]


def fetch_downloads(name: str, token: str) -> int:
    """Sum download counts for every asset of every published release."""
    total = 0
    page = 1
    while True:
        path = (
            f"/repos/{quote(LOGIN, safe='')}/{quote(name, safe='')}/releases"
            f"?per_page=100&page={page}"
        )
        request = urllib.request.Request(REST_URL + path, headers=auth_headers(token))
        releases = request_json(request, allow_404=True)
        if not releases:
            return total
        for release in releases:
            total += sum(asset["download_count"] for asset in release["assets"])
        if len(releases) < 100:
            return total
        page += 1


def fetch_repositories(token: str) -> list[dict]:
    """Fetch every public repository owned by LOGIN, following pagination."""
    repositories: list[dict] = []
    after = None
    while True:
        data = graphql(QUERY, {"login": LOGIN, "after": after}, token)
        user = data.get("user")
        if user is None:
            raise SystemExit(f"GitHub user '{LOGIN}' was not found.")
        connection = user["repositories"]
        repositories.extend(node for node in connection["nodes"] if node)
        if not connection["pageInfo"]["hasNextPage"]:
            return repositories
        after = connection["pageInfo"]["endCursor"]


def summarize(node: dict, downloads: int) -> dict:
    """Flatten one repository node into the fields shown in the dashboard."""
    tags = [tag["name"] for tag in node["refs"]["nodes"] if tag]
    language = node["primaryLanguage"]
    return {
        "name": node["name"],
        "description": (node["description"] or "").strip(),
        "url": node["url"],
        "homepage": node["homepageUrl"],
        "language": language["name"] if language else None,
        "stars": node["stargazerCount"],
        "downloads": downloads,
        "open_issues": node["issues"]["totalCount"],
        "latest_tag": tags[0] if tags else None,
        "tags": tags,
        "archived": node["isArchived"],
        "fork": node["isFork"],
        "updated": node["updatedAt"][:10],
    }


def markdown_cell(value: str | None) -> str:
    if not value:
        return "—"
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def render(projects: list[dict], generated: str) -> str:
    """Build the markdown block that lives between the monitor markers."""
    ordered = sorted(
        projects,
        key=lambda p: (-p["stars"], -p["downloads"], p["name"].lower()),
    )
    totals = {
        "repositories": len(projects),
        "stars": sum(p["stars"] for p in projects),
        "downloads": sum(p["downloads"] for p in projects),
        "issues": sum(p["open_issues"] for p in projects),
    }

    lines = [
        f"_Last synced from the GitHub API on {generated} — refreshes daily._",
        "",
        "| Metric | Total |",
        "|:---|---:|",
        f"| Public repositories | {totals['repositories']:,} |",
        f"| Stars | {totals['stars']:,} |",
        f"| Release asset downloads | {totals['downloads']:,} |",
        f"| Open issues | {totals['issues']:,} |",
        "",
        f"## Repositories ({len(ordered)})",
        "",
        "| Project | Description | Language | Stars | Downloads | Open issues | Latest tag | Updated |",
        "|:---|:---|:---|---:|---:|---:|:---|:---|",
    ]

    for project in ordered:
        label = project["name"]
        if project["archived"]:
            label += " (archived)"
        elif project["fork"]:
            label += " (fork)"
        lines.append(
            "| "
            + " | ".join(
                [
                    f"[{label}]({project['url']})",
                    markdown_cell(project["description"]),
                    markdown_cell(project["language"]),
                    f"{project['stars']:,}",
                    f"{project['downloads']:,}",
                    f"{project['open_issues']:,}",
                    markdown_cell(project["latest_tag"]),
                    project["updated"],
                ]
            )
            + " |"
        )

    return "\n".join(lines)


def replace_monitored_block(block: str) -> bool:
    """Replace the text between markers; return True when README changed."""
    text = README_PATH.read_text(encoding="utf-8")
    if START_MARKER not in text or END_MARKER not in text:
        raise SystemExit(f"README.md must contain both {START_MARKER} and {END_MARKER}")
    before, rest = text.split(START_MARKER, 1)
    _, after = rest.split(END_MARKER, 1)
    updated = f"{before}{START_MARKER}\n{block}\n{END_MARKER}{after}"
    if updated == text:
        return False
    README_PATH.write_text(updated, encoding="utf-8")
    return True


def main() -> int:
    token = get_token()
    projects = [
        summarize(node, fetch_downloads(node["name"], token))
        for node in fetch_repositories(token)
    ]
    if not projects:
        raise SystemExit(f"No public repositories found for '{LOGIN}'.")

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps(projects, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    changed = replace_monitored_block(render(projects, generated))
    state = "updated" if changed else "unchanged"
    print(f"README {state}: {len(projects)} repositories monitored for {LOGIN}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
