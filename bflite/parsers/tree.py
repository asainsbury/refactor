"""Indentation tree for IOS / NX-OS style configs."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Line:
    text: str
    indent: int
    children: list["Line"] = field(default_factory=list)

    @property
    def words(self) -> list[str]:
        return self.text.split()

    def find(self, *prefix: str) -> list["Line"]:
        n = len(prefix)
        return [c for c in self.children if [w.lower() for w in c.words[:n]] == [p.lower() for p in prefix]]


def build_tree(text: str) -> Line:
    root = Line("", -1)
    stack = [root]
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\r\n")
        i += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("!") or stripped.startswith("#"):
            continue
        low = stripped.lower()
        # banners: "banner motd ^C ... ^C" may span lines starting at column 0
        if low.startswith("banner ") and len(stripped.split()) >= 3:
            delim_tok = stripped.split()[2]
            delim = delim_tok[:2] if delim_tok.startswith("^") else delim_tok[0]
            remainder = stripped.split(None, 2)[2][len(delim):]
            if delim not in remainder:
                while i < len(lines) and delim not in lines[i]:
                    i += 1
                i += 1
            continue
        # crypto certificate blobs end with 'quit'
        if low.startswith("certificate ") and stack[-1].text.lower().startswith("crypto pki certificate chain"):
            while i < len(lines) and lines[i].strip() != "quit":
                i += 1
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        node = Line(stripped, indent)
        while stack[-1].indent >= indent:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)
    return root
