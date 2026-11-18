"""Read the referee's append-only score sheet."""

import json
from pathlib import Path


def rankings(path):
    records = json.loads(Path(path).read_text(encoding="utf-8"))["matches"]
    versions = {}
    latest = {}
    for raw in records:
        match = raw["match"]
        revision = raw["revision"]
        home, away = raw["home"], raw["away"]
        hs, aws = raw["home_score"], raw["away_score"]
        if not isinstance(match, str) or not match or type(revision) is not int or revision < 1:
            raise ValueError("invalid score identity")
        if not all(isinstance(team, str) and team for team in (home, away)) or home == away:
            raise ValueError("invalid participants")
        if type(hs) is not int or type(aws) is not int or min(hs, aws) < 0 or hs == aws:
            raise ValueError("a completed match needs a winner")
        candidate = (home, away, hs, aws)
        previous = versions.setdefault(match, {}).get(revision)
        if previous is not None and candidate != previous:
            raise ValueError(f"conflicting score at {match}/{revision}")
        versions[match][revision] = candidate
        if revision > latest.get(match, {}).get("revision", 0):
            latest[match] = {
                "match": match,
                "revision": revision,
                "team": home if hs > aws else away,
                "result": {"home": home, "away": away, "home_score": hs, "away_score": aws},
            }
    return latest
