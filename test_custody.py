"""custody tests -- the four rules, each of which costs something to hold.

These are not coverage. Each one pins a promise that would be quietly
profitable to break: that a stale input stops the model rather than annotating
it, that content is not kept, that approval is evidenced rather than assumed,
and that the tool never grades its own AI favourably.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'attest'))
ATTEST = HERE.parent / 'attest' / 'attest.py'
KEY = 'test-key-not-a-real-one'


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A scratch ledger and key, so tests never touch the real chain."""
    monkeypatch.setenv('ATTEST_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('ATTEST_KEY', KEY)
    for mod in ('attest', 'custody'):
        sys.modules.pop(mod, None)
    import custody
    return custody, tmp_path / 'ledger.jsonl'


def _csv(p, text):
    p.write_text(text.strip() + '\n', encoding='utf-8')
    return p


def _verify(ledger):
    return subprocess.run([sys.executable, str(ATTEST), '--ledger', str(ledger), 'verify'],
                          capture_output=True, text=True,
                          env={**os.environ, 'ATTEST_KEY': KEY})


# ---------------------------------------------------------------- rule 1
def test_stale_input_stops_the_model_rather_than_annotating_it(env, tmp_path):
    """The placement is the whole idea. A warning attached to a finished draft
    is a note nobody reads; a refusal cannot be ignored."""
    custody, ledger = env
    src = _csv(tmp_path / 'old.csv', 'date,amount\n2020-01-01,5')
    ran = []

    with pytest.raises(custody.Refused) as e:
        with custody.observe('summary', inputs=[src], max_input_lag_bdays=1,
                             ledger=ledger) as run:
            ran.append(True)                      # must never execute
            run.output('a confident paragraph about old numbers')

    assert ran == [], 'the body ran despite stale input'
    assert any('STALE INPUT' in p for p in e.value.problems)

    recs = custody.read(ledger)
    assert [r['kind'] for r in recs] == ['ai-refused']
    assert recs[0]['ran'] is False


def test_a_missing_input_is_refused_too(env, tmp_path):
    custody, ledger = env
    with pytest.raises(custody.Refused):
        with custody.observe('summary', inputs=[tmp_path / 'nope.csv'],
                             max_input_lag_bdays=5, ledger=ledger):
            pass
    assert custody.read(ledger)[0]['kind'] == 'ai-refused'


def test_fresh_input_runs_and_records_what_it_could_have_seen(env, tmp_path):
    custody, ledger = env
    import datetime as dt
    today = dt.date.today().isoformat()
    src = _csv(tmp_path / 'fresh.csv', f'date,amount\n{today},5')

    with custody.observe('summary', inputs=[src], model='claude-opus-5',
                         max_input_lag_bdays=1, ledger=ledger) as run:
        run.output('todays figure is 5')

    (rec,) = custody.read(ledger)
    assert rec['kind'] == 'ai-run' and rec['produced_output'] is True
    assert rec['model'] == 'claude-opus-5'
    assert rec['inputs'][0]['content_date'] == today
    assert rec['inputs'][0]['sha256']


# ---------------------------------------------------------------- rule 2
def test_content_is_hashed_and_not_kept(env, tmp_path):
    """A business cannot hand its prompts and customer data to a vendor in
    order to prove its AI behaved."""
    custody, ledger = env
    secret = 'Mrs Alvarez owes 4,200 dollars on invoice 91'

    with custody.observe('dunning', prompt=secret, ledger=ledger) as run:
        run.output(secret)

    raw = ledger.read_text(encoding='utf-8')
    assert 'Alvarez' not in raw and '4,200' not in raw
    rec = json.loads(raw.splitlines()[0])
    assert len(rec['output']['sha256']) == 64
    assert rec['output']['bytes'] == len(secret.encode())
    assert 'text' not in rec['output']


def test_keeping_the_text_is_opt_in_and_marked(env):
    custody, ledger = env
    with custody.observe('dunning', ledger=ledger, keep_text=True) as run:
        run.output('kept on purpose')
    rec = custody.read(ledger)[0]
    assert rec['output']['text'] == 'kept on purpose'
    assert rec['output']['text_kept_deliberately'] is True


# ---------------------------------------------------------------- rule 3
def test_approval_is_a_separate_signed_event_and_never_assumed(env):
    custody, ledger = env
    with custody.observe('summary', ledger=ledger) as run:
        run.output('x')
    run_id = custody.read(ledger)[0]['id']

    assert custody.summarize(custody.read(ledger))['unapproved'] == 1

    custody.approve(run_id, by='Colin', note='checked the totals', ledger=ledger)
    s = custody.summarize(custody.read(ledger))
    assert s['approved'] == 1 and s['unapproved'] == 0

    ap = [r for r in custody.read(ledger) if r['kind'] == 'ai-approved'][0]
    assert ap['by'] == 'Colin' and ap['run_id'] == run_id


def test_an_approval_must_name_a_person(env):
    custody, ledger = env
    with pytest.raises(ValueError):
        custody.approve('abc', by='   ', ledger=ledger)


# ---------------------------------------------------------------- rule 4
def test_it_records_outcomes_and_never_claims_the_ai_was_right(env):
    custody, ledger = env
    with custody.observe('forecast', ledger=ledger,
                         falsifier='any total differs from the ledger by more than $1') as run:
        run.output({'total': 100})
    run_id = custody.read(ledger)[0]['id']

    s = custody.summarize(custody.read(ledger))
    assert s['scored'] == 0 and s['unscored'] == 1, 'an unscored run must not count as correct'
    assert 'accuracy' not in s

    custody.resolve(run_id, 'wrong', evidence='off by $40', ledger=ledger)
    s = custody.summarize(custody.read(ledger))
    assert s['scored'] == 1 and s['wrong'] == 1


def test_an_outcome_must_be_one_of_the_three_words(env):
    custody, _ = env
    with pytest.raises(ValueError):
        custody.resolve('abc', 'pretty good')


# ---------------------------------------------------------------- the rest
def test_a_run_that_produced_nothing_says_so(env):
    """The founding failure of this whole company: it ran, it reported success,
    and it produced nothing."""
    custody, ledger = env
    with custody.observe('summary', ledger=ledger):
        pass
    rec = custody.read(ledger)[0]
    assert rec['produced_output'] is False
    assert custody.summarize([rec])['produced_nothing'] == 1


def test_an_exception_is_recorded_and_still_raised(env):
    custody, ledger = env
    with pytest.raises(ZeroDivisionError):
        with custody.observe('summary', ledger=ledger) as run:
            run.output('partial')
            1 / 0
    rec = custody.read(ledger)[0]
    assert 'division' in rec['error']


def test_policy_can_require_an_agent_to_declare_its_inputs(env, tmp_path):
    """An agent declared as grounded, running on nothing declared, is this
    tool's own failure mode wearing its badge."""
    custody, ledger = env
    pol = tmp_path / 'custody.toml'
    pol.write_text('[agent.summary]\nrequire_inputs = true\n', encoding='utf-8')

    with pytest.raises(custody.Refused) as e:
        with custody.observe('summary', ledger=ledger, policy=pol):
            pass
    assert any('must declare its inputs' in p for p in e.value.problems)

    # a different agent is unaffected by another agent's rule
    with custody.observe('other', ledger=ledger, policy=pol) as run:
        run.output('fine')


def test_a_broken_policy_file_fails_loudly_rather_than_disabling_every_gate(env, tmp_path):
    custody, _ = env
    pol = tmp_path / 'custody.toml'
    pol.write_text('this is not toml = = =', encoding='utf-8')
    with pytest.raises(RuntimeError, match='could not be read'):
        custody.load_policy(pol)


def test_custody_receipts_keep_attests_chain_verifiable(env):
    """custody writes into attest's ledger. If that broke verification, the
    accountability tool would be the thing that destroyed the evidence."""
    custody, ledger = env
    with custody.observe('a', ledger=ledger) as run:
        run.output('one')
    with custody.observe('b', ledger=ledger) as run:
        run.output('two')
    custody.approve(custody.read(ledger)[0]['id'], by='Colin', ledger=ledger)

    r = _verify(ledger)
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'chain intact' in r.stdout


def test_a_filename_is_content_and_can_be_redacted(env, tmp_path):
    """Rule 2 said content is hashed and not kept -- and then paths were stored
    whole. `Alvarez-dispute.csv` names a customer and their problem before
    anyone opens the file, so the report's promise that it could be handed to an
    outsider was false for any company whose filenames mean something."""
    custody, ledger = env
    import datetime as dt
    src = tmp_path / 'Alvarez-dispute.csv'
    src.write_text(f'date,amount\n{dt.date.today().isoformat()},1\n', encoding='utf-8')
    pol = tmp_path / 'custody.toml'
    pol.write_text('[default]\nredact_paths = true\n', encoding='utf-8')

    with custody.observe('summary', inputs=[src], ledger=ledger, policy=pol) as r:
        r.output('x')

    raw = ledger.read_text(encoding='utf-8')
    assert 'Alvarez' not in raw
    entry = custody.read(ledger)[0]['inputs'][0]
    assert entry['path_redacted'] is True and entry['suffix'] == '.csv'
    assert len(entry['path_sha256']) == 64
    assert 'path' not in entry
    # still useful: the same file fingerprints the same way every run
    with custody.observe('summary', inputs=[src], ledger=ledger, policy=pol) as r:
        r.output('y')
    assert custody.read(ledger)[1]['inputs'][0]['path_sha256'] == entry['path_sha256']


def test_paths_are_kept_by_default_because_it_is_your_own_ledger(env, tmp_path):
    custody, ledger = env
    import datetime as dt
    src = tmp_path / 'invoices.csv'
    src.write_text(f'date,amount\n{dt.date.today().isoformat()},1\n', encoding='utf-8')
    with custody.observe('summary', inputs=[src], ledger=ledger) as r:
        r.output('x')
    assert 'invoices.csv' in custody.read(ledger)[0]['inputs'][0]['path']


def test_concurrent_runs_do_not_lose_receipts_or_fork_the_chain(env):
    """The worst defect this tool could have, found by testing rather than
    reasoning. Every receipt links to a hash of the previous line, so two
    writers that read the same "last line" both claim the same predecessor.

    Measured before the fix, 40 runs across 12 threads: 32 receipts survived --
    eight vanished -- and the chain failed with 24 problems. Concurrency is the
    normal case here, not an edge case: the premise is a business running many
    AI actions, and several finishing at once is Tuesday. A tamper-evident
    ledger that corrupts under ordinary load is worse than none, because it
    fails in a way indistinguishable from tampering.
    """
    import concurrent.futures as cf
    custody, ledger = env

    def one(i):
        with custody.observe(f'agent-{i}', ledger=ledger) as r:
            r.output(f'result {i}')

    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        list(ex.map(one, range(40)))

    lines = [ln for ln in ledger.read_text(encoding='utf-8').splitlines() if ln.strip()]
    assert len(lines) == 40, f'{40 - len(lines)} receipt(s) lost to a write race'

    ids = {r['id'] for r in custody.read(ledger)}
    assert len(ids) == 40, 'receipt ids collided'

    r = _verify(ledger)
    assert r.returncode == 0, r.stdout + r.stderr


def test_tampering_with_a_custody_receipt_is_detected(env):
    custody, ledger = env
    with custody.observe('a', ledger=ledger) as run:
        run.output('one')

    rec = json.loads(ledger.read_text(encoding='utf-8').splitlines()[0])
    rec['agent'] = 'something-else'
    ledger.write_text(json.dumps(rec, sort_keys=True, separators=(',', ':')) + '\n',
                      encoding='utf-8')

    assert _verify(ledger).returncode != 0
