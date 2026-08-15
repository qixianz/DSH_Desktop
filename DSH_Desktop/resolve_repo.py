#!/usr/bin/env python3
"""Resolve the dsh repository path for DSH_Desktop tooling (00_env.bat).

Order:
  1. paths.env "REPO_DIR" (written by 01/00 bat, value relative to ROOT)
  2. project-config.json "projectPath" (explicit override, abs or rel to DSH_Desktop/)
  3. auto-detect: a sibling directory of DSH_Desktop/ containing package.json + .git
  4. fallback: <DSH_Desktop parent>/deepseek-harness
Prints one absolute path to stdout.
"""
import json
import os

CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "project-config.json")
BASE = os.path.dirname(CFG)
PARENT = os.path.dirname(BASE)


def _from_paths_file() -> str | None:
    """Read REPO_DIR from paths.env (written by 01/00 bat).

    paths.env lives in BUILD_DIR (= this script's dir); all values are
    relative to ROOT (= BUILD_DIR's parent). Returns None when missing."""
    try:
        with open(os.path.join(BASE, "paths.env"), encoding="utf-8") as f:
            data = {}
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                data[k.strip()] = v.strip()
    except Exception:
        return None
    rel = data.get("REPO_DIR")
    if not rel:
        return None
    root = os.path.dirname(BASE)
    cand = os.path.abspath(os.path.join(root, rel))
    return cand if os.path.isfile(os.path.join(cand, "package.json")) else None


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
    p = _from_paths_file() or _load_override() or _detect()
    print(p or os.path.abspath(os.path.join(PARENT, "deepseek-harness")))


if __name__ == "__main__":
    main()
