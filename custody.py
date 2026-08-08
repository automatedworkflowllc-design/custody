#!/usr/bin/env python3
"""custody -- a chain of custody for work done by AI.

THE QUESTION IT ANSWERS: what did the model actually read, what did it produce,
who approved it, and did it later turn out to be right?
THE BLIND SPOT IT HAS: it records what a program declared and what changed on
disk. It cannot see inside the model, and it does not know whether an answer is
true -- only whether the condition you fixed in advance was later met.

WHY THIS EXISTS. Every company is now putting AI into real workflows, and
almost none can answer a board, an auditor, an insurer or an angry customer
asking "what did it do, and how do you know it was right?" The vendors shipping
the agents are the last people who will build the thing that grades them. That
gap is the product.

WHAT IT IS NOT. Not a prompt logger, not an eval harness. Those record what the
model said. This records what the model was ENTITLED to say: which sources it
could have seen, whether those sources were current, what it produced, and
which human signed it off.

FOUR RULES, each of which costs something to hold:

  1. STALE INPUT MEANS THE MODEL DOES NOT RUN. The check happens before the
     call, not after. A staleness warning stapled to a finished draft is a note
     nobody reads; a refusal cannot be ignored. The refusal is itself a signed
     receipt -- the record of what we declined to do is not the one gap in the
     chain.

  2. CONTENT IS HASHED, NOT KEPT. A business cannot hand its prompts and
     customer data to a vendor, and should not have to in order to prove its AI
     behaved. Receipts carry fingerprints and sizes. Keeping the text is opt-in
     per run, never the default, and never silent.

  3. APPROVAL IS RECORDED, NEVER ASSUMED. "A human reviewed it" is the claim
     most often made and least often evidenced. An approval is a separate,
     separately-signed event naming a person and a time. No approval receipt
     means it was not approved, and the report says so rather than leaving a
     blank the reader will fill in charitably.

  4. IT NEVER SAYS THE AI WAS RIGHT. It records the condition that would prove
     it wrong, fixed before the outcome is known, and whether that condition
     was later met. A tool that scored its own AI favourably would be worth
     exactly nothing.

The chain, signature and staleness arithmetic are attest's, imported rather
than reimplemented. Two copies of a trust primitive that disagree are worse
than one, and this project has already watched that happen twice.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'attest'))
import attest                                                 # noqa: E402

SPEC = 'custody/0.1'
DEFAULT_POLICY = 'custody.toml'


class Refused(RuntimeError):
    """Raised instead of running the model. Carries the reasons, already recorded."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__('; '.join(self.problems))


def _now() -> str:
    return dt.datetime.now().isoformat(timespec='seconds')


def _digest(value):
    """(sha256, bytes) for whatever the model produced.

    Deliberately accepts an object rather than demanding a string: callers hand
    back dicts and dataclasses, and forcing them to serialise first is how a
    tool gets wrapped in a helper that stringifies differently at each call
    site, so the same output hashes two ways.
    """
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode('utf-8')
    else:
        raw = json.dumps(value, sort_keys=True, default=str).encode('utf-8')
    return hashlib.sha256(raw).hexdigest(), len(raw)


def load_policy(path=None) -> dict:
    """Per-company rules, in a file a person can read.

    The personalisation pattern that already proved itself: canary went from
    unusable to usable because one plain text file let its owner say which
    columns were meant to be constant. Nothing in the data could have told it.
    The same holds here -- only this company knows how stale is too stale for
    its own numbers, and which agents may never act unreviewed.
    """
    p = pathlib.Path(path or os.environ.get('CUSTODY_POLICY') or DEFAULT_POLICY)
    if not p.exists():
        return {}
    try:
        import tomllib
        return tomllib.loads(p.read_text(encoding='utf-8'))
    except Exception as e:                                    # noqa: BLE001
        # Fail loudly. A policy that silently evaluated to "no rules" would turn
        # every gate off at once, which is the most dangerous possible way for a
        # typo to behave.
        raise RuntimeError(f'custody: policy file {p} could not be read: {e}') from e


def _rule(policy: dict, agent: str, key: str, default=None):
    """Agent-specific rule if present, else the default section, else default."""
    agents = policy.get('agent') or {}
    if agent in agents and key in agents[agent]:
        return agents[agent][key]
    return (policy.get('default') or {}).get(key, default)


def _ledger_path(ledger=None) -> pathlib.Path:
    return pathlib.Path(ledger) if ledger else attest.HOME / 'ledger.jsonl'


_THREAD_LOCK = threading.Lock()


@contextlib.contextmanager
def _locked(ledger: pathlib.Path):
    """Serialise read-prev / sign / append across threads AND processes.

    Found by testing rather than reasoning, and it was the worst defect this
    tool could have had: 40 concurrent runs produced 39 receipts -- one lost
    outright -- and the chain failed verification with 64 problems. Every
    receipt links to a hash of the previous line, so two writers that read the
    same "last line" both claim the same predecessor and the chain forks.

    Concurrency is the normal case here, not an edge case: the premise is a
    business running many AI actions, and several finishing at once is Tuesday.
    A tamper-evident ledger that corrupts itself under ordinary load is worse
    than no ledger, because it fails in a way that looks exactly like tampering.

    Both locks are needed. The thread lock covers workers inside one process;
    the file lock covers separate processes, and the OS releases it if a process
    dies -- which a hand-rolled lockfile would not.
    """
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger.with_name(ledger.name + '.lock')
    with _THREAD_LOCK:
        fh = open(lock_path, 'a+b')
        try:
            _acquire(fh)
            yield
        finally:
            try:
                _release(fh)
            finally:
                fh.close()


def _acquire(fh) -> None:
    if os.name == 'nt':
        import msvcrt
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                # LK_LOCK already retries for ~10s before raising. Keep waiting
                # rather than writing anyway: a delayed receipt is recoverable,
                # a forked chain is not.
                time.sleep(0.05)
    else:
        import fcntl
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _release(fh) -> None:
    try:
        if os.name == 'nt':
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _write(receipt: dict, ledger: pathlib.Path) -> dict:
    with _locked(ledger):
        receipt['prev'] = attest._last_line_hash(ledger)
        receipt['hmac'] = attest._sign(receipt)
        with ledger.open('a', encoding='utf-8', newline='\n') as fh:
            fh.write(json.dumps(receipt, sort_keys=True, separators=(',', ':')) + '\n')
            fh.flush()
            os.fsync(fh.fileno())
    return receipt


class Run:
    """One observed piece of AI work. Created by observe(); not built directly."""

    def __init__(self, agent, ledger, policy, model, prompt, falsifier, keep_text):
        self.agent = agent
        self.id = secrets.token_hex(8)
        self._ledger = ledger
        self._policy = policy
        self._model = model
        self._prompt = prompt
        self._falsifier = falsifier
        self._keep_text = keep_text
        self._inputs = []
        self._output = None
        self._started = _now()
        self.receipt = None

    def output(self, value) -> None:
        """Record what the model produced. Hashed unless keep_text was set."""
        sha, size = _digest(value)
        self._output = {'sha256': sha, 'bytes': size}
        if self._keep_text:
            # Opt-in AND marked, so nobody reading the ledger later has to guess
            # whether a missing text field means "not kept" or "was empty".
            self._output['text'] = value if isinstance(value, str) else repr(value)
            self._output['text_kept_deliberately'] = True

    def _finish(self, error=None) -> dict:
        r = {
            'spec': SPEC, 'kind': 'ai-run', 'id': self.id, 'agent': self.agent,
            'started': self._started, 'finished': _now(),
            'model': self._model, 'inputs': self._inputs,
            'output': self._output, 'approved': False,
        }
        if self._prompt is not None:
            sha, size = _digest(self._prompt)
            r['prompt'] = {'sha256': sha, 'bytes': size}
        if self._falsifier:
            r['falsifier'] = self._falsifier
        if error:
            r['error'] = str(error)[:300]
        # A run that produced nothing is not a success, and a reader should not
        # have to infer that from an absent field.
        r['produced_output'] = self._output is not None
        self.receipt = _write(r, self._ledger)
        return self.receipt


class observe:
    """Wrap a piece of AI work so it leaves a signed, checkable record.

        with custody.observe('invoice-summary',
                             inputs=['exports/invoices.csv'],
                             model='claude-opus-5',
                             falsifier='any total differs from the ledger by >$1') as run:
            run.output(model_call(...))

    If a declared input is missing or staler than policy allows, this raises
    Refused BEFORE the body runs -- the model is never called -- and the refusal
    is written to the ledger.
    """

    def __init__(self, agent, inputs=(), model=None, prompt=None, falsifier=None,
                 max_input_lag_bdays=None, ledger=None, policy=None, keep_text=False):
        self.agent = agent
        self._inputs = [str(p) for p in inputs]
        self._ledger = _ledger_path(ledger)
        self._policy = policy if isinstance(policy, dict) else load_policy(policy)
        self._model = model
        self._prompt = prompt
        self._falsifier = falsifier
        self._keep_text = keep_text
        lag = max_input_lag_bdays
        if lag is None:
            lag = _rule(self._policy, agent, 'max_input_lag_bdays')
        self._lag = lag
        self.run = None

    def __enter__(self) -> Run:
        entries, problems = attest._check_inputs(self._inputs, self._lag)

        if _rule(self._policy, self.agent, 'require_inputs', False) and not self._inputs:
            # An agent declared as grounded, running on nothing declared, is the
            # failure this tool exists to catch wearing the tool's own badge.
            problems.append('POLICY: this agent must declare its inputs, and declared none')

        if problems:
            _write({'spec': SPEC, 'kind': 'ai-refused', 'id': secrets.token_hex(8),
                    'agent': self.agent, 'at': _now(), 'model': self._model,
                    'inputs': entries, 'problems': problems, 'ran': False}, self._ledger)
            raise Refused(problems)

        self.run = Run(self.agent, self._ledger, self._policy, self._model,
                       self._prompt, self._falsifier, self._keep_text)
        self.run._inputs = entries
        return self.run

    def __exit__(self, exc_type, exc, tb):
        self.run._finish(error=exc)
        return False          # never swallow the caller's exception


def approve(run_id: str, by: str, note: str = '', ledger=None) -> dict:
    """Record that a named person signed off on a specific run.

    A separate event on purpose. Mutating the original receipt would break the
    chain and, worse, would make an approval indistinguishable from something
    the system decided for itself.
    """
    if not by or not by.strip():
        raise ValueError('custody: an approval must name a person')
    return _write({'spec': SPEC, 'kind': 'ai-approved', 'id': secrets.token_hex(8),
                   'run_id': run_id, 'by': by.strip(), 'note': note.strip(),
                   'at': _now()}, _ledger_path(ledger))


def resolve(run_id: str, outcome: str, evidence: str = '', ledger=None) -> dict:
    """Record how a run's falsifier actually turned out."""
    if outcome not in ('correct', 'wrong', 'unclear'):
        raise ValueError("custody: outcome must be 'correct', 'wrong' or 'unclear'")
    return _write({'spec': SPEC, 'kind': 'ai-resolved', 'id': secrets.token_hex(8),
                   'run_id': run_id, 'outcome': outcome,
                   'evidence': evidence.strip(), 'at': _now()}, _ledger_path(ledger))


def read(ledger=None) -> list:
    """Every custody receipt in the ledger, oldest first."""
    lp = _ledger_path(ledger)
    if not lp.exists():
        return []
    out = []
    for line in lp.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(r.get('spec', '')).startswith('custody/'):
            out.append(r)
    return out


def main(argv=None) -> int:
    """The human half of the tool.

    The library is for the program: it observes runs as they happen. Everything
    here is something a PERSON does afterwards -- approving, recording how it
    turned out, printing the page. Splitting them this way is deliberate: an
    approval a program could grant itself would not be an approval.
    """
    import argparse
    ap = argparse.ArgumentParser(prog='custody', description=__doc__.splitlines()[0])
    ap.add_argument('--ledger', help='ledger file (default: the attest ledger)')
    sub = ap.add_subparsers(dest='cmd', required=True)

    s = sub.add_parser('show', help='recent AI runs, newest first')
    s.add_argument('-n', type=int, default=20)

    a = sub.add_parser('approve', help='record that a person signed off on a run')
    a.add_argument('run_id')
    a.add_argument('--by', required=True, help='the person, named')
    a.add_argument('--note', default='')

    r = sub.add_parser('resolve', help='record how a run turned out against its falsifier')
    r.add_argument('run_id')
    r.add_argument('outcome', choices=['correct', 'wrong', 'unclear'])
    r.add_argument('--evidence', default='')

    p = sub.add_parser('report', help='build the page you hand a board or an auditor')
    p.add_argument('--out', default='custody-report.html')
    p.add_argument('--org', default='')

    args = ap.parse_args(argv)
    receipts = read(args.ledger)

    if args.cmd == 'show':
        rows = [x for x in receipts if x.get('kind') in ('ai-run', 'ai-refused')]
        approved = {x.get('run_id') for x in receipts if x.get('kind') == 'ai-approved'}
        if not rows:
            print('custody: no AI runs recorded yet.')
            return 0
        for x in reversed(rows[-args.n:]):
            if x['kind'] == 'ai-refused':
                print(f'{x["at"]}  {x["id"]}  {x["agent"]:<22} REFUSED  {x["problems"][0][:70]}')
            else:
                mark = 'approved' if x['id'] in approved else 'NOT APPROVED'
                made = 'output' if x.get('produced_output') else 'PRODUCED NOTHING'
                print(f'{x["started"]}  {x["id"]}  {x["agent"]:<22} {made:<16} {mark}')
        return 0

    if args.cmd == 'approve':
        rec = approve(args.run_id, by=args.by, note=args.note, ledger=args.ledger)
        print(f'[custody] {args.run_id} approved by {rec["by"]}')
        return 0

    if args.cmd == 'resolve':
        resolve(args.run_id, args.outcome, evidence=args.evidence, ledger=args.ledger)
        print(f'[custody] {args.run_id} recorded as {args.outcome}')
        return 0

    if args.cmd == 'report':
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import report as _report
        st = _report.render(receipts, pathlib.Path(args.out), org=args.org)
        print(f'[custody] {st["runs"]} run(s), {st["refused"]} refused, '
              f'{st["unapproved"]} unapproved, {st["wrong"]} wrong -> {args.out}')
        # Exit 1 when something in the record needs a person. A reporting tool
        # that always exits 0 is one a scheduler learns to ignore.
        return 1 if (st['unapproved'] or st['wrong'] or st['produced_nothing']) else 0

    return 2


def summarize(receipts) -> dict:
    """The numbers a report may state. Counted here so a page cannot invent them."""
    runs = [r for r in receipts if r.get('kind') == 'ai-run']
    refused = [r for r in receipts if r.get('kind') == 'ai-refused']
    approvals = {r.get('run_id') for r in receipts if r.get('kind') == 'ai-approved'}
    resolutions = [r for r in receipts if r.get('kind') == 'ai-resolved']
    scored = [r for r in resolutions if r.get('outcome') in ('correct', 'wrong')]
    return {
        'runs': len(runs),
        'refused': len(refused),
        'approved': sum(1 for r in runs if r['id'] in approvals),
        'unapproved': sum(1 for r in runs if r['id'] not in approvals),
        'produced_nothing': sum(1 for r in runs if not r.get('produced_output')),
        'scored': len(scored),
        'wrong': sum(1 for r in scored if r.get('outcome') == 'wrong'),
        'unscored': len(runs) - len(scored),
    }


if __name__ == "__main__":
    sys.exit(main())
