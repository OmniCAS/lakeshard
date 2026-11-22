"""Read the import header of a Lean source file without running anything from it."""
from __future__ import annotations

import re

IMPORT_RE = re.compile(r"^(?:public\s+|private\s+)?(?:meta\s+)?import\s+(?:all\s+)?(.+)$")


def parse_imports(text: str) -> list[str]:
    """Return the modules imported in the header of `text` (in order, duplicates kept)."""
    imps: list[str] = []
    depth = 0  # block-comment nesting
    for line in text.splitlines():
        s = line.strip()
        while True:
            if depth > 0:
                j, k = s.find("-/"), s.find("/-")
                if k != -1 and (j == -1 or k < j):
                    depth += 1
                    s = s[k + 2:]
                    continue
                if j == -1:
                    s = ""
                    break
                depth -= 1
                s = s[j + 2:].strip()
                continue
            if s.startswith("/-"):
                depth += 1
                s = s[2:]
                continue
            break
        if not s or s.startswith("--"):
            continue
        if s in ("module", "prelude") or s.startswith("module ") or s.startswith("module--"):
            continue
        m = IMPORT_RE.match(s)
        if m:
            body = m.group(1).split("--")[0]
            if "/-" in body:
                body = body.split("/-")[0]
            imps.extend(body.split())
            continue
        break
    return imps


def parse_file(path: str) -> tuple[list[str], int, int]:
    """(imports, number of lines, number of bytes) of a source file."""
    with open(path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8", errors="replace")
    return parse_imports(text), text.count("\n") + (0 if text.endswith("\n") or not text else 1), len(data)
