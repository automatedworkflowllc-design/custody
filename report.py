#!/usr/bin/env python3
"""The page you hand a board, an auditor, an insurer, or a customer who asked.

The argument it has to make is uncomfortable on purpose, and it is the same one
/proof/ makes about our own jobs: a record containing only successes is
marketing. So the refusals, the unapproved runs and the ones that turned out
wrong come FIRST, and the reassuring number comes last, if it comes at all.

THREE THINGS IT REFUSES TO DO, each of which would make it prettier:

  It will not print an accuracy rate over a handful of scored runs. A
  percentage over four outcomes is noise wearing a lab coat, and the first
  person to quote it in a deck would be quoting nothing. Same floor as attest's
  predictions, for the same reason: ten.

  It will not count an unscored run as a correct one. Most AI work is never
  checked against anything, and the honest report says how much -- silence
  about the unscored majority is how a 3-for-3 record gets presented as
  perfect.

  It will not show prompts or outputs. It only ever had their fingerprints, and
  a report that leaked what the ledger deliberately did not keep would undo the
  reason the ledger is safe to keep at all.
"""
from __future__ import annotations

import datetime as dt
import html
import pathlib

import custody

MIN_SCORED_FOR_A_RATE = 10          # same floor as attest predictions, same reason

CSS = """
body{font:15px/1.6 ui-sans-serif,system-ui,sans-serif;background:#FBFAF3;color:#211D14;
margin:0;padding:2.4rem 1.3rem 4rem;max-width:58rem;margin-inline:auto}
h1{font-size:1.5rem;margin:0 0 .3rem} h2{font-size:1rem;margin:2rem 0 .5rem}
.sub{color:#5C5645;margin:0 0 1.4rem;font-size:.93rem}
.tot{display:flex;gap:.5rem;flex-wrap:wrap;margin:1.2rem 0 1.6rem}
.t{border:1px solid #E4DFD1;border-radius:.6rem;background:#F4F1E8;padding:.5rem .8rem}
.t b{display:block;font-family:ui-monospace,Consolas,monospace;font-size:1.25rem}
.t span{font-size:.68rem;color:#5C5645;text-transform:uppercase;letter-spacing:.07em}
table{border-collapse:collapse;width:100%;font-size:.88rem;margin-top:.4rem}
td{border-top:1px solid #E4DFD1;padding:.5rem .6rem;vertical-align:top}
.m{font-family:ui-monospace,Consolas,monospace;font-size:.82em;color:#5C5645;white-space:nowrap}
.bad{color:#B4452C;font-weight:600} .warn{color:#8A6A16;font-weight:600}
.ok{color:#1E7A47}
.box{border:1px solid #E4DFD1;background:#F4F1E8;border-radius:.6rem;padding:1rem 1.2rem;
margin:1.3rem 0}
.box.hard{border-left:4px solid #B4452C}
"""


def _fmt(ts: str) -> str:
    return (ts or '').replace('T', ' ')[:16]


def _source_label(entry: dict) -> str:
    """What a source is called on the page, honouring redaction.

    A filename is content. `Alvarez-dispute.csv` names a customer and their
    problem before anyone opens it, so when the policy redacts paths this shows
    a stable short fingerprint instead. Stable matters: an auditor can still see
    that twelve runs read the same source and the thirteenth did not, without
    learning what it was called.
    """
    if entry.get('path_redacted'):
        return (f'<span class="m">file #{html.escape(entry.get("path_sha256", "")[:8])}'
                f'{html.escape(entry.get("suffix") or "")}</span>')
    return html.escape(pathlib.Path(entry.get('path', '')).name)


def _rate_line(s: dict) -> str:
    """What we are allowed to say about accuracy, and why, printed on the page."""
    if s['scored'] < MIN_SCORED_FOR_A_RATE:
        return (f'<b>No accuracy rate is shown.</b> {s["scored"]} run(s) have been checked '
                f'against a condition set for them in advance, and a percentage over fewer '
                f'than {MIN_SCORED_FOR_A_RATE} outcomes is noise. Counts are given instead.')
    right = s['scored'] - s['wrong']
    return (f'<b>{right} of {s["scored"]} checked runs met the condition set in advance</b> '
            f'({round(100 * right / s["scored"])}%). The other {s["unscored"]} run(s) were '
            f'never checked against anything, and are counted here as neither.')


def render(receipts, out_path: pathlib.Path, org: str = '') -> dict:
    s = custody.summarize(receipts)
    runs = [r for r in receipts if r.get('kind') == 'ai-run']
    refused = [r for r in receipts if r.get('kind') == 'ai-refused']
    approvals = {r.get('run_id'): r for r in receipts if r.get('kind') == 'ai-approved'}
    outcomes = {r.get('run_id'): r for r in receipts if r.get('kind') == 'ai-resolved'}

    refusal_rows = ''.join(
        f'<tr><td class="m">{_fmt(r.get("at"))}</td><td>{html.escape(r.get("agent") or "?")}</td>'
        f'<td>{html.escape("; ".join(r.get("problems") or []))}</td></tr>'
        for r in reversed(refused)) or (
        '<tr><td colspan=3>No run has been stopped. That is only good news if the gate is '
        'switched on &mdash; an agent that declares no inputs can never be refused.</td></tr>')

    wrong_rows = ''.join(
        f'<tr><td class="m">{_fmt(o.get("at"))}</td>'
        f'<td>{html.escape(next((x.get("agent") or "?" for x in runs if x["id"] == rid), "?"))}</td>'
        f'<td>{html.escape(o.get("evidence") or "no evidence recorded")}</td></tr>'
        for rid, o in outcomes.items() if o.get('outcome') == 'wrong') or (
        '<tr><td colspan=3>None recorded. Note what that does and does not mean: '
        f'{s["unscored"]} run(s) were never checked against anything.</td></tr>')

    run_rows = []
    for r in reversed(runs):
        ap = approvals.get(r['id'])
        oc = outcomes.get(r['id'])
        who = (f'<span class="ok">{html.escape(ap["by"])}</span>' if ap
               else '<span class="warn">not approved</span>')
        if oc:
            cls = {'wrong': 'bad', 'correct': 'ok'}.get(oc['outcome'], 'warn')
            verdict = f'<span class="{cls}">{html.escape(oc["outcome"])}</span>'
        else:
            verdict = '<span class="m">not checked</span>'
        produced = ('<span class="bad">produced nothing</span>'
                    if not r.get('produced_output') else
                    f'<span class="m">{(r.get("output") or {}).get("sha256", "")[:12]}</span>')
        srcs = ', '.join(_source_label(i) for i in (r.get('inputs') or [])) \
            or '<span class="warn">none declared</span>'
        run_rows.append(
            f'<tr><td class="m">{_fmt(r.get("started"))}</td>'
            f'<td>{html.escape(r.get("agent") or "?")}</td>'
            f'<td class="m">{html.escape(r.get("model") or "-")}</td>'
            f'<td>{srcs}</td><td>{produced}</td><td>{who}</td><td>{verdict}</td></tr>')

    unapproved_note = (
        f'<br><br><b>{s["unapproved"]} run(s) were never approved by anyone.</b> That is '
        f'recorded rather than left blank, because a blank invites a charitable reading.'
        if s['unapproved'] else '')
    nothing_note = (
        f'<br><br><b>{s["produced_nothing"]} run(s) finished without producing anything.</b> '
        f'A job that reports success and produces nothing is the failure this whole system '
        f'exists to catch.' if s['produced_nothing'] else '')

    # Filenames are content too, and this page claimed it was safe to hand to an
    # outsider while printing them. Say which of the two situations the reader is
    # actually in rather than asserting the flattering one.
    all_inputs = [i for r in runs + refused for i in (r.get('inputs') or [])]
    redacted = all_inputs and all(i.get('path_redacted') for i in all_inputs)
    path_note = (
        '<br><br><b>Filenames are redacted.</b> Sources appear as stable fingerprints, so this '
        'page can be handed to someone outside the company without showing them what the files '
        'are called &mdash; a filename is content too. The same source fingerprints the same way '
        'every run, so it is still possible to see that several runs read one source and another '
        'did not.'
        if redacted else
        '<br><br><b>Filenames are shown, and a filename is content.</b> Before handing this page '
        'outside the company, consider whether names like <code>Q3-client-dispute.csv</code> '
        'reveal something. Set <code>redact_paths = true</code> in the policy and sources appear '
        'as fingerprints instead.')

    title = 'What our AI did' + (f' &mdash; {html.escape(org)}' if org else '')
    out_path.write_text(f"""<!doctype html><meta charset=utf8>
<title>{title}</title><style>{CSS}</style>
<h1>{title}</h1>
<p class="sub">Every action taken by an automated model, what it was allowed to read, who signed
it off, and how it turned out. Generated {dt.datetime.now().isoformat(timespec='seconds')} from a
signed, hash-chained ledger.</p>

<div class="tot">
  <div class="t"><b>{s['runs']}</b><span>runs recorded</span></div>
  <div class="t"><b>{s['refused']}</b><span>stopped before running</span></div>
  <div class="t"><b>{s['unapproved']}</b><span>never approved</span></div>
  <div class="t"><b>{s['produced_nothing']}</b><span>produced nothing</span></div>
  <div class="t"><b>{s['wrong']}</b><span>later found wrong</span></div>
</div>

<div class="box hard"><b>The uncomfortable part, which is the point.</b> {_rate_line(s)}
{unapproved_note}{nothing_note}</div>

<h2>Stopped before the model ran</h2>
<p class="sub">These never reached the model, because the data it would have read was missing or
out of date. A refusal is the most valuable line in this report: it is the wrong answer that was
never written.</p>
<table><tr><td><b>when</b></td><td><b>agent</b></td><td><b>why it was stopped</b></td></tr>
{refusal_rows}</table>

<h2>Turned out wrong</h2>
<p class="sub">Judged against a condition fixed <em>before</em> the outcome was known, so it could
not be reinterpreted afterwards.</p>
<table><tr><td><b>when</b></td><td><b>agent</b></td><td><b>evidence</b></td></tr>
{wrong_rows}</table>

<h2>Every run</h2>
<table><tr><td><b>when</b></td><td><b>agent</b></td><td><b>model</b></td><td><b>could read</b></td>
<td><b>produced</b></td><td><b>approved by</b></td><td><b>outcome</b></td></tr>
{''.join(run_rows) or '<tr><td colspan=7>No runs recorded yet.</td></tr>'}</table>

<div class="box"><b>What this cannot prove.</b> It records what a program declared and what changed
on disk. It shows which sources a run was entitled to read and that those bytes have not changed
since &mdash; not that the model read them, or read them correctly. It cannot see inside the model.
And it never claims an answer was true: only whether the condition set in advance was later met.
<br><br><b>What it deliberately does not contain.</b> No prompts and no outputs. The ledger keeps
fingerprints and sizes, never the text.
{path_note}</div>
""", encoding='utf-8')
    return s
