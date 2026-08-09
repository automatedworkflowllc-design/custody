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


def _today():
    """Today in ISO form, for fixtures that must be fresh rather than frozen."""
    import datetime as dt
    return dt.date.today().isoformat()


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


def test_a_gate_that_cannot_find_a_date_must_not_report_freshness(env, tmp_path):
    """Measured, not imagined. A 2.7MB CSV containing nothing but 2019 dates
    passed a one-business-day limit: attest stops scanning for dates above 2MB,
    so the comparison had nothing to compare and the run proceeded.

    Big exports are exactly what a business feeds an AI, so the gate failed open
    on its most important case -- silently, leaving someone certain they had a
    gate they did not have."""
    custody, ledger = env
    over_cap = tmp_path / 'big.csv'
    over_cap.write_text('date,amount\n' + '2019-01-01,1\n' * 200000, encoding='utf-8')
    assert over_cap.stat().st_size > 2_000_000

    with pytest.raises(custody.Refused) as e:
        with custody.observe('summary', inputs=[over_cap], max_input_lag_bdays=1,
                             ledger=ledger):
            pass
    assert 'UNVERIFIABLE INPUT' in e.value.problems[0]
    assert 'too large to scan' in e.value.problems[0]

    undated = tmp_path / 'nodates.csv'
    undated.write_text('a,b\n1,2\n', encoding='utf-8')
    with pytest.raises(custody.Refused) as e2:
        with custody.observe('summary', inputs=[undated], max_input_lag_bdays=1,
                             ledger=ledger):
            pass
    assert 'no date was found' in e2.value.problems[0]


def test_an_undated_input_is_only_a_problem_when_a_limit_was_asked_for(env, tmp_path):
    """Recording is not judging. Without a limit, an undated input is simply
    recorded -- turning that into a refusal would break every existing job that
    declares inputs for provenance rather than for a gate."""
    custody, ledger = env
    undated = tmp_path / 'nodates.csv'
    undated.write_text('a,b\n1,2\n', encoding='utf-8')

    with custody.observe('summary', inputs=[undated], ledger=ledger) as r:
        r.output('fine')                       # no limit -> no gate -> runs

    pol = tmp_path / 'custody.toml'
    pol.write_text('[default]\nallow_undated_inputs = true\n', encoding='utf-8')
    with custody.observe('summary', inputs=[undated], max_input_lag_bdays=1,
                         ledger=ledger, policy=pol) as r:
        r.output('accepted deliberately')

    assert len([x for x in custody.read(ledger) if x['kind'] == 'ai-run']) == 2


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


# ------------------------------------------------- wrap: AI work that is a command
# The library API assumes you own the Python process calling the model. None of
# our real AI jobs look like that -- they are scheduled commands invoking a model
# CLI -- so these pin that the same four rules survive the trip through argv.

def test_wrap_stale_input_never_launches_the_command(env, tmp_path):
    """Rule 1 through a subprocess. The strong claim is not that the run is
    flagged: it is that the process is never spawned at all."""
    custody, ledger = env
    src = _csv(tmp_path / 'old.csv', 'date,amount\n2019-03-04,5')
    sentinel = tmp_path / 'IT_RAN'

    rc, receipt = custody.wrap(
        [sys.executable, '-c', f'open(r"{sentinel}", "w").write("x")'],
        'scout', inputs=[src], max_input_lag_bdays=1, ledger=ledger)

    assert rc == 4
    assert not sentinel.exists(), 'the command ran despite a stale input'
    assert receipt is None
    last = json.loads(ledger.read_text(encoding='utf-8').splitlines()[-1])
    assert last['kind'] == 'ai-refused' and last['ran'] is False


def test_wrap_reports_a_command_that_succeeded_and_produced_nothing(env, tmp_path):
    """Exit 0 with no output is the silent failure the whole stack is pointed
    at, so it must not inherit the command's cheerful exit code."""
    custody, ledger = env
    src = _csv(tmp_path / 'now.csv', f'date,amount\n{_today()},5')

    rc, receipt = custody.wrap([sys.executable, '-c', 'pass'], 'scout',
                               inputs=[src], outputs=[tmp_path / 'never.txt'],
                               ledger=ledger)

    assert rc == 3
    assert receipt['produced_output'] is False


def test_wrap_passes_through_the_exit_code_of_a_command_that_failed(env, tmp_path):
    """A command that failed already told the truth about itself. Returning 3
    there would report 'exited 0 and produced nothing' about an exit 9."""
    custody, ledger = env
    src = _csv(tmp_path / 'now.csv', f'date,amount\n{_today()},5')

    rc, _ = custody.wrap([sys.executable, '-c', 'import sys; sys.exit(9)'], 'scout',
                         inputs=[src], outputs=[tmp_path / 'never.txt'], ledger=ledger)

    assert rc == 9, 'a failing command must not be relabelled as a silent failure'


def test_wrap_hashes_the_command_rather_than_keeping_it(env, tmp_path):
    """Rule 2 where it is easiest to break: these jobs pass the prompt inline
    with -p, so a receipt that stored the command line would store the prompt."""
    custody, ledger = env
    src = _csv(tmp_path / 'now.csv', f'date,amount\n{_today()},5')
    out = tmp_path / 'out.txt'
    secret = 'summarise-this-customers-overdue-balance'

    rc, receipt = custody.wrap(
        [sys.executable, '-c', f'open(r"{out}", "w").write("{secret}")'],
        'scout', inputs=[src], outputs=[out], ledger=ledger)

    assert rc == 0 and receipt['produced_output'] is True
    assert receipt['command']['program'] == pathlib.Path(sys.executable).name
    assert len(receipt['command']['sha256']) == 64
    raw = ledger.read_text(encoding='utf-8')
    assert secret not in raw, 'the command line reached the ledger in clear text'


def test_wrap_accepts_a_directory_when_the_filename_varies_per_run(env, tmp_path):
    """Real agents write YYYY-MM-DD-<topic>.md, so they cannot declare a fixed
    --out. attest already solved this; custody must agree with it."""
    custody, ledger = env
    src = _csv(tmp_path / 'now.csv', f'date,amount\n{_today()},5')
    briefs = tmp_path / 'briefs'
    briefs.mkdir()

    rc, receipt = custody.wrap(
        [sys.executable, '-c', f'open(r"{briefs / "dated-brief.md"}", "w").write("today")'],
        'scout', inputs=[src], out_dirs=[briefs], ledger=ledger)

    assert rc == 0 and receipt['produced_output'] is True


def test_wrap_does_not_count_a_byte_identical_rewrite_as_production(env, tmp_path):
    """The one that separates a real check from a mtime check: a job that
    rewrites yesterday's file unchanged has produced nothing, and saying
    otherwise is precisely the false success this stack exists to catch."""
    custody, ledger = env
    src = _csv(tmp_path / 'now.csv', f'date,amount\n{_today()},5')
    briefs = tmp_path / 'briefs'
    briefs.mkdir()
    existing = briefs / 'yesterday.md'
    existing.write_text('same bytes', encoding='utf-8')

    rc, receipt = custody.wrap(
        [sys.executable, '-c', f'open(r"{existing}", "w").write("same bytes")'],
        'scout', inputs=[src], out_dirs=[briefs], ledger=ledger)

    assert rc == 3, 'an unchanged rewrite was counted as output'
    assert receipt['produced_output'] is False
