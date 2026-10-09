"""Minimal read-only Jira Cloud client: GET requests and the JQL search endpoint only.

Credentials come from jira_secrets.toml locally, from environment variables
(JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT) in GitHub Actions,
or from a mapping such as Streamlit's st.secrets["jira"]. The token is never printed or logged.
"""
from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import requests


class Jira:
    def __init__(self, base_url: str, email: str, token: str, project: str = "MYDSUP"):
        self.base = base_url.rstrip("/")
        self.project = project
        self.s = requests.Session()
        self.s.auth = (email, token)
        self.s.headers.update({"Accept": "application/json", "Content-Type": "application/json"})

    def __repr__(self) -> str:  # never show the token
        return f"Jira({self.base}, project={self.project})"

    @classmethod
    def from_file(cls, path: Path | None = None) -> "Jira":
        if os.environ.get("JIRA_API_TOKEN"):
            return cls(os.environ["JIRA_BASE_URL"], os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"],
                       os.environ.get("JIRA_PROJECT", "MYDSUP"))
        p = path or Path(__file__).parent / "jira_secrets.toml"
        c = tomllib.loads(p.read_text(encoding="utf-8"))["jira"]
        return cls.from_mapping(c)

    @classmethod
    def from_mapping(cls, c) -> "Jira":
        return cls(c["base_url"], c["email"], c["api_token"], c.get("project", "MYDSUP"))

    # Read-only lock: GET anything, POST only to Jira's search endpoints (they take the query as a body
    # and change nothing). Any other request is refused before it leaves this machine.
    READ_ONLY_POST = ("/rest/api/3/search/jql", "/rest/api/3/search/approximate-count")

    def _call(self, method: str, path: str, **kw):
        if method != "GET" and not (method == "POST" and path in self.READ_ONLY_POST):
            raise PermissionError(f"Blocked: this client is read-only and will not send {method} {path} to Jira.")
        r = None
        for attempt in range(6):
            try:
                r = self.s.request(method, self.base + path, timeout=60, **kw)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == 5:
                    raise
                time.sleep(min(2 ** attempt, 60))
                continue
            if r.status_code == 429 or r.status_code >= 500:      # rate limited or a hiccup: back off and retry
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            if r.status_code in (401, 403):
                raise PermissionError(f"Jira refused {method} {path} ({r.status_code}). Check the email/token and permissions.")
            r.raise_for_status()
            return r.json() if r.content else {}
        r.raise_for_status()

    def get(self, path: str, **params):
        return self._call("GET", path, params=params)

    def post(self, path: str, body: dict):
        """Only used for read-only search endpoints (Jira's search takes its query as a POST body)."""
        return self._call("POST", path, json=body)

    def search(self, jql: str, fields: list[str], expand: str | None = None, page: int = 100):
        """Enhanced JQL search (/rest/api/3/search/jql) with nextPageToken paging. Yields issues."""
        token = None
        while True:
            body = {"jql": jql, "fields": fields, "maxResults": page}
            if expand:
                body["expand"] = expand
            if token:
                body["nextPageToken"] = token
            res = self.post("/rest/api/3/search/jql", body)
            yield from res.get("issues", [])
            token = res.get("nextPageToken")
            if not token or res.get("isLast"):
                return

    def changelog(self, key: str):
        start = 0
        while True:
            res = self.get(f"/rest/api/3/issue/{key}/changelog", startAt=start, maxResults=100)
            vals = res.get("values", [])
            yield from vals
            start += len(vals)
            if res.get("isLast", True) or not vals:
                return

    def comments_full(self, key: str):
        """Comment metadata: id, author (account object), created, public. The comment text is dropped here."""
        start = 0
        while True:
            res = self.get(f"/rest/api/3/issue/{key}/comment", startAt=start, maxResults=100, expand="properties")
            vals = res.get("comments", [])
            for c in vals:
                public = True
                for prop in c.get("properties", []):
                    if prop.get("key") == "sd.public.comment":
                        public = not (prop.get("value") or {}).get("internal", False)
                yield {"id": c["id"], "author": c.get("author"), "created": c.get("created"), "public": public}
            start += len(vals)
            if start >= res.get("total", 0) or not vals:
                return
