# roomy

**Decide what fits in a context window, and say why the rest did not.**

Counting tokens is the easy part — every library does it. The hard part is that
once you are over budget you have to *drop something*, and most tools drop
silently: they truncate the tail of the prompt, or they keep adding files until
they hit the limit and stop. Both leave you with an agent that answered from a
prompt you never saw.

roomy packs under an explicit budget and returns, for every candidate, either
the reason it is in or the reason it is out.

```bash
python roomy.py ../mcpfromzero --budget 6000 --reserve 1000
```

```
budget 6000  reserve 1000  used 4820  headroom 180
------------------------------------------------------------------------
IN  README.md                                   2871  fits (priority 2)
IN  docs/NOTICE.md                               412  fits (priority 1)
IN  python/mcpfromzero/__main__.py                27  fits (priority 0)
...
out conformance/run.py                          3694  no room left: 3694 needed, 180 free
out typescript/src/tools.ts                     4371  no room left: 4371 needed, 180 free
out python/mcpfromzero/tools.py                 4666  no room left: 4666 needed, 180 free
------------------------------------------------------------------------
9 in, 14 out
```

## As a library

```python
from roomy import pack, Candidate

plan = pack([
    Candidate("spec.md",   spec_text,   priority=2, required=True),
    Candidate("api.py",    api_text,    priority=1),
    Candidate("legacy.py", legacy_text),
], budget=8000, reserve=1000)

for d in plan.decisions:
    print(d.name, "IN " if d.included else "out", d.reason)
```

`pack` raises `WontFit` if a **required** candidate does not fit. It does not
return a plan that is missing something you declared you could not work
without — that failure would otherwise arrive as a confident, wrong answer.

## 🔴 Three things it is opinionated about

**The reserve is not optional.** The window is shared with the output. A prompt
that "fits" in 6,000 tokens does not fit if the model then needs 1,000 to
answer, and that failure arrives as a truncated reply rather than as an error.

**The estimate is biased high, on purpose.** roomy ships no tokenizer —
vendoring one ties the tool to a single model family and is wrong for every
other. The four-characters-per-token rule of thumb is an average over English
prose, and agent prompts are made of code. Measured over the
[mcpfromzero](https://github.com/dev48v/mcpfromzero) repository: **206,658
characters, 65,859 tokens — 27% more than the rule of thumb predicts.** An
over-count wastes a little budget; an under-count overflows the window.

**The packing is a heuristic, and the gap is measured rather than denied.**
Packing a budget optimally is knapsack. roomy sorts by priority, then size,
then name — and the Agent Lab page measures what that gives up against an
exhaustive search (82.8% of optimal at one budget). A packing you can explain
line by line and reproduce exactly is worth more here than a few percent of
window, because the output is read by a human deciding what to include.

The sort ends on the **name** so the result never depends on the order the
caller happened to build the list in.

## Tests

```bash
python -m unittest test_roomy -v     # 36 tests
```

They guard the four invariants worth guarding: the budget is never exceeded,
every candidate gets a decision, the result does not depend on input order, and
a required item that does not fit raises rather than vanishing.

## Install

Nothing to install. Python 3.8+, standard library only.

---

MIT © Devanshu Biswas. Built as [Agent Lab](https://dev48.in) Vol 5 #02.
