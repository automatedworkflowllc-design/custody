# custody

**A chain of custody for work done by AI.**

Your board is going to ask. So is your auditor, your insurer, and eventually a
customer who got a bad answer:

> *What did it do, and how do you know it was right?*

Right now almost nobody can answer that. Chat logs show what the model **said**.
custody records what it was **entitled to say** — which sources it could see,
whether they were current, what it produced, which human signed it off, and
whether it later turned out to be wrong.

```python
import custody

with custody.observe('invoice-summary',
                     inputs=['exports/invoices.csv'],
                     model='claude-opus-5',
                     max_input_lag_bdays=1,
                     falsifier='any total differs from the ledger by more than $1') as run:
    run.output(your_model_call(...))
```

If `invoices.csv` is stale, **that block never executes.** The model is not
called, and the refusal is recorded.

## Four rules, each of which costs something

**1 · Stale input means the model does not run.**
The check happens *before* the call. A staleness warning stapled to a finished
draft is a note nobody reads; a refusal cannot be ignored. And the refusal is
itself a signed receipt — the record of what you declined to do is not the one
gap in the chain.

**2 · Content is hashed, never kept.**
You cannot hand your prompts and customer data to a vendor in order to prove
your AI behaved. Receipts carry fingerprints and sizes. Keeping the text is
opt-in per run, and marked when it happens. Filenames are content too — set
`redact_paths` and sources become stable fingerprints instead.

**3 · Approval is recorded, never assumed.**
"A human reviewed it" is the claim most often made and least often evidenced.
An approval is a separate signed event naming a person. No approval receipt
means it was not approved, and the report says so rather than leaving a blank
that a reader will fill in charitably.

**4 · It never says the AI was right.**
It records the condition that would prove it wrong, fixed *before* the outcome
is known, and whether that condition was later met. There is no accuracy field,
and under ten scored runs the report refuses to print a percentage at all. A
tool that graded its own AI favourably would be worth nothing.

## The report

```
custody report --out board-report.html --org "Northwind Services"
```

The page leads with the uncomfortable part, because a record containing only
successes is marketing: what was refused, what nobody approved, what turned out
wrong. The reassuring number comes last, if it comes at all. It exits **1** when
something in the record needs a person — a reporting tool that always exits 0 is
one a scheduler learns to ignore.

## The rest of the CLI

The library is for your program; these are the things a *person* does afterwards.
That split is deliberate: an approval your program could grant itself would not
be an approval.

```
custody show                        what ran, what was refused, what nobody approved
custody approve <id> --by "Name"    a person, named
custody resolve <id> wrong --evidence "all three renewed"
```

## Policy: the part only you know

`custody.toml`, in plain text you can read:

```toml
[default]
max_input_lag_bdays = 2      # how stale is too stale, for your numbers
redact_paths        = true   # filenames are content

[agent.pricing-update]
require_inputs      = true   # this one may never run ungrounded
max_input_lag_bdays = 0      # and only on same-day data
```

Nothing in the data can tell custody how fresh *your* figures need to be, or
which agents must never act unreviewed. A malformed policy raises rather than
quietly evaluating to "no rules" — the most dangerous way a typo can behave.

## Install

```
git clone https://github.com/automatedworkflowllc-design/attest
pip install ./attest
git clone https://github.com/automatedworkflowllc-design/custody
pip install ./custody
```

Install from the repositories, not by name: `attest` and `custody` on PyPI both
belong to unrelated projects. These publish as `awllc-attest` and
`awllc-custody`.

## What it cannot prove

It records what a program declared and what changed on disk. It shows which
sources a run was entitled to read, and that those bytes have not changed
since — **not** that the model read them, or read them correctly. It cannot see
inside the model. And it never claims an answer was true, only whether the
condition set in advance was later met.

Saying that plainly is the point. A tool that implied more would be asking for
exactly the trust it exists to replace.

## How it fits with the others

| tool | the question it answers |
|---|---|
| **custody** | what did the AI do, and was it right? |
| [attest][] | did this job run, and did it produce what it claimed? |
| [flatline][] | is this data still carrying information? |
| [canary][] | what is wrong in the file that just landed? |

custody is built **on** attest: same hash chain, same signatures, same
business-day staleness arithmetic, imported rather than reimplemented. Two
copies of a trust primitive diverge the first time only one gets fixed, so
there is one of each. They can safely write the same ledger at the same moment.

## Tests

```
python -m pytest -q
```

29 tests, and they are the argument rather than coverage. Each pins a promise
that would be quietly profitable to break: the body must not execute on stale
input, a customer name written through the wrapper must not appear in the
ledger, an unscored run must not be counted as correct, and 40 concurrent runs
must produce 40 receipts with the chain intact — measured at 32 before that was
fixed.

---

MIT. Built by [Automated Workflow](https://automatedworkflowllc.com).

[attest]: https://github.com/automatedworkflowllc-design/attest
[flatline]: https://github.com/automatedworkflowllc-design/flatline
[canary]: https://github.com/automatedworkflowllc-design/canary
