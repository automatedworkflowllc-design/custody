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

import datetime as dt
import hashlib
import json
import os
import pathlib
import secrets
import subprocess
import sys
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


def _redact_inputs(entries):
    """Replace file paths with fingerprints, keeping the audit value.

    Rule 2 says content is hashed and not kept -- but the PATHS were being
    stored whole, and a path is content. `2026-Q3/client-dispute.csv` names a
    customer and their problem before anyone opens the file, and the report
    promises it can be handed to an outsider. That promise was false for any
    company whose filenames mean something, which is most of them.

    The fingerprint is kept rather than dropped so the record stays useful: the
    same file hashes the same way every run, so an auditor can still see that
    twelve runs read one source and a thirteenth read a different one. They
    simply cannot see what it was called.
    """
    out = []
    for e in entries:
        e = dict(e)
        p = e.pop('path', '')
        e['path_sha256'] = hashlib.sha256(str(p).encode('utf-8')).hexdigest()
        e['suffix'] = pathlib.Path(str(p)).suffix
        e['path_redacted'] = True
        out.append(e)
    return out


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


def _write(receipt: dict, ledger: pathlib.Path) -> dict:
    """Append via attest, which owns the chain, the signature AND the lock.

    custody briefly carried its own copy of the locking logic. That is the
    drift this project keeps catching in itself: two implementations of one
    trust primitive, guaranteed to diverge the first time only one of them is
    fixed. There is exactly one writer now, and both tools share it -- which
    is also why an attest job and a custody run can safely append to the same
    ledger at the same moment.
    """
    return attest._append(receipt, ledger)


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
        self._command = None
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
        if self._command:
            r['command'] = self._command
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
        entries, problems = attest._check_inputs(
            self._inputs, self._lag,
            _rule(self._policy, self.agent, 'allow_undated_inputs', False))
        if _rule(self._policy, self.agent, 'redact_paths', False):
            entries = _redact_inputs(entries)

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


def wrap(command, agent, inputs=(), model=None, outputs=(), out_dirs=(),
         falsifier=None, max_input_lag_bdays=None, ledger=None, policy=None,
         keep_text=False, _run=None) -> tuple:
    """Observe AI work that is a COMMAND rather than a Python program.

    The library API assumes you own the process that calls the model. Most real
    AI work does not look like that: it is a scheduled command invoking a model
    CLI, and until this existed custody could not touch a single one of ours.
    Discovered by trying to use it on the real jobs rather than on an example.

    It calls observe() rather than reimplementing the four rules, for the same
    reason custody imports attest instead of copying its hash chain: two copies
    of a trust primitive diverge the first time only one gets fixed.

    Returns (exit_code, receipt). Exit codes match attest deliberately -- a
    scheduler should not have to learn a second vocabulary:
      4  refused; the input was stale, and THE COMMAND NEVER RAN
      3  the command reported success and produced nothing it declared
      *  otherwise the command's own exit code, passed through untouched
    """
    runner = _run or subprocess.run
    # Snapshot declared directories BEFORE the run. Agents whose output
    # filename varies per run (a dated brief, a per-topic file) cannot declare
    # a fixed --out, and attest already solved this; reuse its snapshot rather
    # than write a second one that will disagree with it later.
    before_dirs = {d: attest._dir_snapshot(str(d)) for d in out_dirs}
    # The command line is content: these jobs pass the prompt inline with -p,
    # so keeping it verbatim would break rule 2 in the very feature meant to
    # demonstrate it. Hash the whole line; keep only the program name in clear.
    cmd_sha, _size = _digest(' '.join(command))
    program = pathlib.Path(command[0]).name if command else ''

    try:
        ctx = observe(agent, inputs=inputs, model=model, falsifier=falsifier,
                      max_input_lag_bdays=max_input_lag_bdays, ledger=ledger,
                      policy=policy, keep_text=keep_text)
        run = ctx.__enter__()
    except Refused:
        # observe() has already written the refusal receipt. Nothing ran.
        return 4, None

    run._command = {'program': program, 'sha256': cmd_sha}
    proc_rc, err = 0, None
    try:
        proc = runner(list(command))
        proc_rc = getattr(proc, 'returncode', 0)
    except OSError as exc:                       # command not found, not runnable
        proc_rc, err = 127, exc

    # Read declared outputs AFTER the run. A job that exits 0 having written
    # nothing is the silent failure this whole stack exists to catch, so it is
    # recorded as producing nothing rather than inheriting the command's 0.
    produced = [pathlib.Path(p) for p in outputs]
    blobs = [p.read_bytes() for p in produced if p.exists() and p.stat().st_size]

    # A declared directory counts as produced only if a file under it was
    # created or CHANGED. Hashes, not mtimes: a job that rewrites yesterday's
    # brief byte for byte has honestly produced nothing new, and mtime would
    # call that success.
    for d in out_dirs:
        after = attest._dir_snapshot(str(d)) or {}
        prior = before_dirs.get(d) or {}
        fresh = sorted(k for k, v in after.items() if prior.get(k) != v)
        for rel in fresh:
            blobs.append((pathlib.Path(d) / rel).read_bytes())

    if blobs:
        run.output(b''.join(blobs))

    ctx.__exit__(type(err) if err else None, err, None)
    receipt = run.receipt
    if err:
        return 127, receipt
    # 3 means "claimed success, produced nothing" -- the silent failure. A
    # command that FAILED already told the truth about itself, so its own code
    # passes through; reporting 3 there would have said "exited 0" about a
    # command that exited 1. Caught the first time this ran.
    if proc_rc == 0 and (outputs or out_dirs) and not blobs:
        return 3, receipt
    return proc_rc, receipt


def main(argv=None) -> int:
    """The human half of the tool, plus one thing a scheduler does.

    The library is for the program: it observes runs as they happen. Most of
    what follows is something a PERSON does afterwards -- approving, recording
    how it turned out, printing the page. Splitting them this way is
    deliberate: an approval a program could grant itself would not be one.

    `wrap` is the exception, and it is not an approval: it is the same
    observation the library performs, for work that is a command rather than a
    Python program.
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

    w = sub.add_parser('wrap', help='run an AI command under custody and record it')
    w.add_argument('--agent', required=True, help='what this run is, e.g. research-scout')
    w.add_argument('--model', help='the model being called, recorded as declared')
    w.add_argument('--in', dest='inputs', action='append', default=[],
                   metavar='PATH', help='a source this run is entitled to read (repeatable)')
    w.add_argument('--out', dest='outputs', action='append', default=[],
                   metavar='PATH', help='a file this run must produce (repeatable)')
    w.add_argument('--out-dir', dest='out_dirs', action='append', default=[],
                   metavar='DIR', help='a directory in which this run must create or '
                                       'change at least one file, for agents whose '
                                       'output filename varies per run (repeatable)')
    w.add_argument('--max-input-lag-bdays', type=int, default=None,
                   help='refuse to run if an input is older than this many business days')
    w.add_argument('--falsifier', help='the condition that would prove this run wrong')
    w.add_argument('--policy', help='custody.toml (default: found beside the ledger)')
    w.add_argument('command', nargs=argparse.REMAINDER,
                   help='-- then the command to run')

    p = sub.add_parser('report', help='build the page you hand a board or an auditor')
    p.add_argument('--out', default='custody-report.html')
    p.add_argument('--org', default='')
    p.add_argument('--redact', action='store_true',
                   help='fingerprint every source filename at render time, for a page '
                        'that will be published rather than handed to the data owner')

    args = ap.parse_args(argv)

    if args.cmd == 'wrap':
        # argparse.REMAINDER keeps the "--" separator; drop it so the command
        # is what the user actually typed after it.
        cmd = list(args.command)
        if cmd and cmd[0] == '--':
            cmd = cmd[1:]
        if not cmd:
            print('custody wrap: no command given (put it after --)', file=sys.stderr)
            return 2
        rc, receipt = wrap(cmd, args.agent, inputs=args.inputs, model=args.model,
                           outputs=args.outputs, out_dirs=args.out_dirs,
                           falsifier=args.falsifier,
                           max_input_lag_bdays=args.max_input_lag_bdays,
                           ledger=args.ledger, policy=args.policy)
        if rc == 4:
            print(f'[custody] REFUSED: {args.agent} did not run -- an input was '
                  f'stale or missing. The model was never called.', file=sys.stderr)
        elif rc == 3:
            print(f'[custody] {args.agent} exited 0 and produced nothing it declared: '
                  f'{", ".join(args.outputs + args.out_dirs)}', file=sys.stderr)
        elif receipt:
            print(f'[custody] {args.agent} recorded as {receipt["id"]}')
        return rc

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
        st = _report.render(receipts, pathlib.Path(args.out), org=args.org,
                            redact=args.redact)
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
