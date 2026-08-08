"""Tests for the page shown to outsiders.

The HTML is not the product; the three refusals are. Each of these would make
the report look better and would make it worthless.
"""
import pathlib
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'attest'))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('ATTEST_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('ATTEST_KEY', 'test-key-not-a-real-one')
    for mod in ('attest', 'custody', 'report'):
        sys.modules.pop(mod, None)
    import custody
    import report
    return custody, report, tmp_path / 'ledger.jsonl'


def _run(custody, ledger, agent='summary', out='x', falsifier=None):
    with custody.observe(agent, ledger=ledger, falsifier=falsifier) as r:
        r.output(out)
    return custody.read(ledger)[-1]['id']


def test_no_accuracy_rate_over_a_handful_of_outcomes(env, tmp_path):
    """A percentage over four outcomes is noise wearing a lab coat, and the
    first person to quote it in a deck would be quoting nothing."""
    custody, report, ledger = env
    for _ in range(3):
        custody.resolve(_run(custody, ledger), 'correct', ledger=ledger)

    out = tmp_path / 'r.html'
    report.render(custody.read(ledger), out)
    text = out.read_text(encoding='utf-8')

    assert 'No accuracy rate is shown' in text
    # the stylesheet legitimately contains width:100%; assert against the BODY,
    # or this repeats tonight's mailto mistake -- matching a string and calling
    # it a meaning.
    body = text.split('</style>', 1)[1]
    assert '100%' not in body and '%)' not in body


def test_a_rate_appears_once_there_is_enough_to_divide_by(env, tmp_path):
    custody, report, ledger = env
    for i in range(12):
        custody.resolve(_run(custody, ledger), 'wrong' if i < 3 else 'correct', ledger=ledger)

    out = tmp_path / 'r.html'
    report.render(custody.read(ledger), out)
    text = out.read_text(encoding='utf-8')
    assert '9 of 12 checked runs met the condition' in text
    assert '75%' in text


def test_unscored_runs_are_never_counted_as_correct(env, tmp_path):
    """Most AI work is never checked against anything. Silence about the
    unscored majority is how a 3-for-3 record gets shown as perfect."""
    custody, report, ledger = env
    for i in range(12):
        rid = _run(custody, ledger)
        if i < 10:
            custody.resolve(rid, 'correct', ledger=ledger)

    out = tmp_path / 'r.html'
    s = report.render(custody.read(ledger), out)
    assert s['scored'] == 10 and s['unscored'] == 2

    text = out.read_text(encoding='utf-8')
    assert '10 of 10 checked runs met the condition' in text
    assert 'The other 2 run(s) were never checked against anything' in text


def test_the_report_never_contains_a_prompt_or_an_output(env, tmp_path):
    """It only ever had fingerprints. A page that leaked what the ledger
    deliberately did not keep would undo the reason the ledger is safe."""
    custody, report, ledger = env
    secret = 'Mrs Alvarez owes 4,200 dollars'
    with custody.observe('dunning', ledger=ledger, prompt=secret, keep_text=True) as r:
        r.output(secret)

    out = tmp_path / 'r.html'
    report.render(custody.read(ledger), out)
    text = out.read_text(encoding='utf-8')
    assert 'Alvarez' not in text and '4,200' not in text


def test_refusals_and_unapproved_runs_lead_the_page(env, tmp_path):
    """A record containing only successes is marketing, so the bad news is
    placed first rather than in a footnote."""
    custody, report, ledger = env
    src = tmp_path / 'old.csv'
    src.write_text('date,amount\n2020-01-01,5\n', encoding='utf-8')
    with pytest.raises(custody.Refused):
        with custody.observe('summary', inputs=[src], max_input_lag_bdays=1, ledger=ledger):
            pass
    _run(custody, ledger)

    out = tmp_path / 'r.html'
    report.render(custody.read(ledger), out)
    text = out.read_text(encoding='utf-8')

    assert text.index('Stopped before the model ran') < text.index('Every run')
    assert 'never approved by anyone' in text
    assert 'STALE INPUT' in text


def test_an_empty_ledger_does_not_read_as_a_clean_bill_of_health(env, tmp_path):
    """Zero refusals is only good news if the gate is switched on."""
    custody, report, ledger = env
    out = tmp_path / 'r.html'
    report.render([], out)
    text = out.read_text(encoding='utf-8')
    assert 'only good news if the gate is switched on' in text


def test_a_run_that_produced_nothing_is_called_out(env, tmp_path):
    custody, report, ledger = env
    with custody.observe('summary', ledger=ledger):
        pass
    out = tmp_path / 'r.html'
    report.render(custody.read(ledger), out)
    text = out.read_text(encoding='utf-8')
    assert 'produced nothing' in text
    assert 'finished without producing anything' in text


def test_the_page_states_what_it_cannot_prove(env, tmp_path):
    custody, report, ledger = env
    _run(custody, ledger)
    out = tmp_path / 'r.html'
    report.render(custody.read(ledger), out)
    text = out.read_text(encoding='utf-8')
    assert 'What this cannot prove' in text
    assert 'cannot see inside the model' in text
