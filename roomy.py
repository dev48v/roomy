"""roomy - decide what fits in a context window, and say why the rest did not.

The problem it solves is not "count tokens". Every library does that. The
problem is that once you are over budget you have to *drop something*, and
most tools drop silently - they truncate the end of the prompt, or they keep
adding files until they hit the limit and stop. Both leave you with an agent
that answered from a prompt you never saw.

roomy packs under an explicit budget and returns, for every candidate, either
the reason it is in or the reason it is out. Nothing is ever dropped quietly.

    from roomy import pack, Candidate
    plan = pack(candidates, budget=8000)
    for d in plan.decisions:
        print(d.name, d.included, d.reason)

Usage:
    python roomy.py <dir> --budget 8000 [--json] [--reserve 500]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# counting
# --------------------------------------------------------------------------- #

# A deliberate, documented approximation. roomy does NOT ship a tokenizer:
# vendoring one would tie the tool to one model family and would be wrong for
# every other. This counter is calibrated to be CONSERVATIVE - it over-counts
# rather than under-counts - because an over-count wastes a little budget and
# an under-count silently blows the window.
_WORDISH = re.compile(r"\w+|[^\w\s]")


def estimate_tokens(text: str) -> int:
    """Approximate token count, biased high.

    Rule of thumb everyone quotes is 4 characters per token. That is an
    average over English prose and it is badly wrong for code, which is what
    goes into agent prompts: identifiers split, punctuation is its own token,
    and indentation is not free.

    So: count word-ish runs and punctuation separately, then add a penalty for
    long identifiers, which tokenizers split into pieces.
    """
    if not text:
        return 0
    pieces = _WORDISH.findall(text)
    n = 0
    for p in pieces:
        if p.isspace():
            continue
        # a long identifier becomes several tokens
        n += 1 + (len(p) - 1) // 6
    # newlines and runs of indentation are tokens too
    n += text.count("\n")
    return n


# --------------------------------------------------------------------------- #
# the model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Candidate:
    """One thing that might go in the prompt."""
    name: str
    text: str
    priority: int = 0          # higher wins; ties broken by name, never by order
    required: bool = False     # if it does not fit, that is an ERROR, not a drop

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class Decision:
    name: str
    tokens: int
    included: bool
    reason: str


@dataclass
class Plan:
    budget: int
    reserve: int
    used: int = 0
    decisions: list = field(default_factory=list)

    @property
    def included(self):
        return [d for d in self.decisions if d.included]

    @property
    def excluded(self):
        return [d for d in self.decisions if not d.included]

    @property
    def headroom(self) -> int:
        return self.budget - self.reserve - self.used

    def names(self):
        return [d.name for d in self.included]


class WontFit(Exception):
    """A required candidate did not fit. Raised rather than returned, because
    a caller that ignores it has built a prompt missing something it declared
    it could not do without."""


# --------------------------------------------------------------------------- #
# packing
# --------------------------------------------------------------------------- #

def pack(candidates, budget: int, reserve: int = 0) -> Plan:
    """Pack under a budget, explaining every decision.

    `reserve` is space held back for the model's own reply and for anything
    the caller appends later. Forgetting it is the single most common way a
    prompt that "fits" does not: the window is shared with the output.

    The order is by (priority desc, tokens asc, name asc). Smaller-first
    within a priority is deliberate - it fits more of the same priority in -
    and the name is there so the result never depends on the order the caller
    happened to build the list in.
    """
    if budget <= 0:
        raise ValueError("budget must be positive")
    if reserve < 0:
        raise ValueError("reserve cannot be negative")
    if reserve >= budget:
        raise ValueError("reserve must leave something to pack into")

    seen = set()
    for c in candidates:
        if c.name in seen:
            raise ValueError("duplicate candidate name: %r" % c.name)
        seen.add(c.name)

    usable = budget - reserve
    plan = Plan(budget=budget, reserve=reserve)

    # required first, in their own pass - a required item must never lose its
    # place to an optional one that merely sorted higher
    required = [c for c in candidates if c.required]
    optional = [c for c in candidates if not c.required]

    for c in sorted(required, key=lambda c: (-c.priority, c.tokens, c.name)):
        if plan.used + c.tokens > usable:
            raise WontFit(
                "%r is required and needs %d tokens, but only %d remain of a "
                "%d budget (%d reserved)"
                % (c.name, c.tokens, usable - plan.used, budget, reserve))
        plan.used += c.tokens
        plan.decisions.append(Decision(c.name, c.tokens, True, "required"))

    for c in sorted(optional, key=lambda c: (-c.priority, c.tokens, c.name)):
        if c.tokens > usable:
            plan.decisions.append(Decision(
                c.name, c.tokens, False,
                "too large for the budget on its own (%d > %d usable)" % (c.tokens, usable)))
        elif plan.used + c.tokens > usable:
            plan.decisions.append(Decision(
                c.name, c.tokens, False,
                "no room left: %d needed, %d free" % (c.tokens, usable - plan.used)))
        else:
            plan.used += c.tokens
            plan.decisions.append(Decision(
                c.name, c.tokens, True, "fits (priority %d)" % c.priority))

    return plan


# --------------------------------------------------------------------------- #
# reading a directory
# --------------------------------------------------------------------------- #

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
TEXT_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".md", ".txt", ".json", ".yml",
            ".yaml", ".toml", ".cfg", ".ini", ".html", ".css", ".sh", ".sql"}


def read_dir(root: str, max_bytes: int = 400_000):
    """Collect text files as candidates. Binary and oversized files are
    skipped here rather than inside pack(), so the packer never has to guess
    what a file is."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext not in TEXT_EXT:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > max_bytes:
                    continue
                with open(full, "r", encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            # README and the entry point are worth more than a random module
            pri = 2 if fn.lower().startswith("readme") else 1 if ext == ".md" else 0
            out.append(Candidate(name=rel, text=text, priority=pri))
    return out


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path")
    ap.add_argument("--budget", type=int, default=8000)
    ap.add_argument("--reserve", type=int, default=0,
                    help="tokens held back for the reply (and anything appended later)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cands = read_dir(args.path)
    if not cands:
        print("nothing readable under %s" % args.path, file=sys.stderr)
        return 1
    try:
        plan = pack(cands, budget=args.budget, reserve=args.reserve)
    except WontFit as e:
        print("WONT FIT: %s" % e, file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "budget": plan.budget, "reserve": plan.reserve, "used": plan.used,
            "headroom": plan.headroom,
            "decisions": [d.__dict__ for d in plan.decisions],
        }, indent=2))
        return 0

    print("budget %d  reserve %d  used %d  headroom %d"
          % (plan.budget, plan.reserve, plan.used, plan.headroom))
    print("-" * 72)
    for d in plan.decisions:
        print("%s %-40s %7d  %s" % ("IN " if d.included else "out", d.name[:40], d.tokens, d.reason))
    print("-" * 72)
    print("%d in, %d out" % (len(plan.included), len(plan.excluded)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
