"""Mirrors of the pybatfish.datamodel objects that appear inside answer frames."""
from __future__ import annotations

from dataclasses import dataclass

_NAME_SPECIAL_CHARS_ = " \t,\\&()[]@" + "!#$%^;?<>={}"


def escape_name(s: str) -> str:
    """Same quoting rule as pybatfish.util.escape_name."""
    if s and (s.startswith('"') or s.startswith("/") or s[0].isdigit()
              or any(s.find(c) > 0 for c in _NAME_SPECIAL_CHARS_)):
        return f'"{s}"'
    return s


@dataclass(frozen=True, order=True)
class Interface:
    """Same attributes, str() and repr() as pybatfish.datamodel.primitives.Interface."""
    hostname: str
    interface: str

    def __str__(self) -> str:
        return f"{escape_name(self.hostname)}[{escape_name(self.interface)}]"

    def __repr__(self) -> str:
        return f"Interface(hostname={self.hostname!r}, interface={self.interface!r})"


class ListWrapper(list):
    """Stand-in for pybatfish's ListWrapper (a list subclass).

    pybatfish's ListWrapper subclasses pandas' FrozenList, whose str() is pandas' pprint_thing. That
    string is what the pipeline's df.astype(str) writes for every list cell, so str() here matches it."""

    def __str__(self) -> str:
        return pd_str(self)


# pandas.io.formats.printing.pprint_thing(quote_strings=True, escape_chars=("\t", "\r", "\n", "'")),
# checked against pandas 3.0.5 in the batfish/allinone image (see docs/BFLITE_README.md).
_PD_ESCAPES = {"\t": r"\t", "\r": r"\r", "\n": r"\n", "'": r"\'"}
PD_MAX_SEQ_ITEMS = 100          # pandas option display.max_seq_items
_PD_NEST_DEPTH = 3              # pandas option display.pprint_nest_depth


def _pd_escape(s: str) -> str:
    for c, r in _PD_ESCAPES.items():
        s = s.replace(c, r)
    return s


def _pd_thing(v, lvl: int, max_items: int = PD_MAX_SEQ_ITEMS) -> str:
    if isinstance(v, dict) and lvl < _PD_NEST_DEPTH:
        items = list(v.items())
        body = ", ".join(f"{_pd_thing(k, lvl + 1)}: {_pd_thing(x, lvl + 1)}" for k, x in items[:max_items])
        return "{" + body + (", ..." if len(items) > max_items else "") + "}"
    if isinstance(v, (list, tuple, set, frozenset)) and lvl < _PD_NEST_DEPTH:
        seq = list(v)
        body = ", ".join(_pd_thing(x, lvl + 1) for x in seq[:max_items])
        if len(seq) > max_items:
            body += ", ..."
        elif isinstance(v, tuple) and len(seq) == 1:
            body += ","
        fmt = "{%s}" if isinstance(v, (set, frozenset)) else ("(%s)" if isinstance(v, tuple) else "[%s]")
        return fmt % body
    if isinstance(v, str):
        return f"'{_pd_escape(v)}'"
    return _pd_escape(str(v))


def pd_str(v) -> str:
    """The text df.astype(str) gives one pybatfish answer cell: lists like pandas prints a ListWrapper
    (strings quoted, other objects via str(), at most 100 items then '...'), everything else str(v)."""
    if isinstance(v, (list, tuple)):
        return _pd_thing(v, 0)
    return str(v)
