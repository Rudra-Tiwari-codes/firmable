"""Thin CKAN Action API client with an on-disk response cache.

Caching every response means a rerun sees exactly the catalogue state the
first run saw, which keeps the shortlist reproducible.
"""

import hashlib
import json
import time

import requests

from . import DATA

DEFAULT_BASE = "https://data.gov.au/data/api/3/action/"
CACHE_DIR = DATA / "cache" / "ckan"
USER_AGENT = "firmable-takehome/0.1 (research; contact via repo)"


class CKAN:
    def __init__(self, base=DEFAULT_BASE, use_cache=True, pause=0.2):
        self.base = base if base.endswith("/") else base + "/"
        self.use_cache = use_cache
        self.pause = pause
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, action, params):
        # The file name is a hash of the whole request (portal + action + parameters), so the same
        # question always lands on the same file and two different questions never collide.
        key = json.dumps([self.base, action, params], sort_keys=True)
        return CACHE_DIR / (hashlib.sha1(key.encode()).hexdigest() + ".json")

    def call(self, action, **params):
        path = self._cache_path(action, params)
        # Cache hit: answer from disk and make no network call at all.
        if self.use_cache and path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        # Up to 3 tries, waiting 1 s then 2 s between them (2 ** attempt).
        for attempt in range(3):
            try:
                r = self.session.get(self.base + action, params=params, timeout=60)
                r.raise_for_status()
                body = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                # A 4xx is an answer (no such resource), so asking again only wastes time.
                status = getattr(getattr(e, "response", None), "status_code", None)
                if attempt == 2 or (status and 400 <= status < 500):
                    raise
                time.sleep(2 ** attempt)
        # CKAN wraps every answer as {"success": bool, "result": ...}; only the result is kept.
        if not body.get("success"):
            raise RuntimeError(f"CKAN {action} failed: {body.get('error')}")
        result = body["result"]
        path.write_text(json.dumps(result), encoding="utf-8")
        time.sleep(self.pause)  # a short pause after each real request, to be polite to a public server
        return result

    def package_search(self, q="*:*", rows=100, start=0, sort=None):
        params = {"q": q, "rows": rows, "start": start}
        if sort:
            params["sort"] = sort
        return self.call("package_search", **params)

    def package_show(self, id):
        return self.call("package_show", id=id)

    def datastore_fields(self, resource_id):
        """Column names for a datastore-backed resource, or None. A failure is cached as well:
        many resources claim a datastore and don't have one, and each one retried on every run."""
        miss = self._cache_path("datastore_search_failed", {"resource_id": resource_id})
        if self.use_cache and miss.exists():
            return None
        try:
            # limit=0 asks for no rows, only the field list: the cheapest way to see a table's columns.
            res = self.call("datastore_search", resource_id=resource_id, limit=0)
        except Exception:
            miss.write_text("null", encoding="utf-8")
            return None
        # Fields starting with "_" (such as _id) are added by the datastore, not by the publisher.
        return [f["id"] for f in res.get("fields", []) if not f["id"].startswith("_")]
