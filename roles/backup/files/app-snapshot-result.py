#!/usr/bin/python3
"""Accept only a completed immutable generation under its approved staging root."""

import json
import os
from pathlib import Path
import re
import sys


def validate(result, root, dataset):
    if result.get("completed") is not True or result.get("dataset") != dataset:
        raise ValueError("app snapshot is incomplete or has the wrong dataset")
    path = result.get("path")
    if not isinstance(path, str) or any(character in path for character in "\n\r"):
        raise ValueError("invalid app-snapshot generation path")
    root_path = Path(root)
    generation = Path(path)
    relative = generation.relative_to(root_path)
    if (len(relative.parts) != 2 or relative.parts[0] != "snapshots"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", relative.parts[1])):
        raise ValueError("app snapshot must name one immutable snapshots/<id> generation")
    if not generation.is_dir() or os.path.realpath(generation) != os.path.normpath(path):
        raise ValueError("app-snapshot generation is missing or symlinked")
    return path


if __name__ == "__main__":
    try:
        payload = sys.stdin.read(65537)
        if len(payload) > 65536:
            raise ValueError("app-snapshot result is too large")
        print(validate(json.loads(payload), sys.argv[1], sys.argv[2]))
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        print("NAS app snapshot did not return a valid completed immutable generation", file=sys.stderr)
        sys.exit(1)
