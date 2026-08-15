#!/usr/bin/env python3
"""Resolve the dsh repository path for Build tooling (env.bat).

Order:
  1. project-config.json "projectPath" (explicit override, abs or rel to Build/)
  2. auto-detect: a sibling directory of Build/ containing package.json + .git
  3. fallback: <Build parent>/Source
Prints one absolute path to stdout.
"""
import json
import os

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project-config.json")
BASE = os.path.dirname(CFG)
PARENT = os.path.dirname(BASE)


def _load_override() -> str | None:
    try:
        with open(CFG, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("projectPath")
        return raw if isinstance(raw, str) and raw else None
    except Exception:
        return None


def _detect() -> str | None:
    try:
        for name in sorted(os.listdir(PARENT)):
            cand = os.path.join(PARENT, name)
            if os.path.abspath(cand) == os.path.abspath(BASE):
                continue
            if (os.path.isdir(cand)
                    and os.path.isfile(os.path.join(cand, "package.json"))
                    and os.path.isdir(os.path.join(cand, ".git"))):
                return os.path.abspath(cand)
    except OSError:
        pass
    return None


def main() -> None:
    p = _load_override()
    if p:
        print(os.path.abspath(os.path.join(BASE, p)))
        return
    p = _detect()
    print(p or os.path.abspath(os.path.join(PARENT, "Source")))


if __name__ == "__main__":
    main()
