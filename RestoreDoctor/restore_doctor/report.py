"""Single-file HTML report for a case: print it or attach it to the ticket."""
from __future__ import annotations

import html
import time
from pathlib import Path

from . import APP_NAME, __version__
from .diagnose import Verdict

CSS = """
:root{--ink:#16181d;--quiet:#5b6170;--line:#d9dce3;--accent:#2563eb;--good:#15803d;--warn:#b45309;--bad:#b91c1c;--bg:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 "Segoe UI",system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 24px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:26px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.meta{color:var(--quiet);font-size:12.5px;margin-top:4px}
.verdict{border:1px solid var(--line);border-left:6px solid var(--accent);border-radius:8px;padding:14px 16px;margin-top:18px}
.verdict.hardware{border-left-color:var(--bad)}.verdict.setup,.verdict.firmware{border-left-color:var(--warn)}.verdict.success{border-left-color:var(--good)}
.verdict h2{border:0;margin:0 0 6px;font-size:17px}
.pill{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:1px 9px;font-size:12px;color:var(--quiet);margin-right:6px}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--quiet);font-weight:600}
.bar{height:6px;background:#e8ebf1;border-radius:3px;overflow:hidden;min-width:80px}.bar i{display:block;height:100%;background:var(--accent)}
ol,ul{margin:6px 0 0 18px;padding:0}li{margin:3px 0}
code,pre{font:12px/1.45 Consolas,ui-monospace,monospace}pre{background:#f5f6f8;border:1px solid var(--line);border-radius:6px;padding:10px;overflow-x:auto;white-space:pre-wrap}
.ev{color:var(--quiet);font-size:12px}
footer{color:var(--quiet);font-size:11.5px;margin-top:30px}
@media print{main{padding:0}details{display:none}}
"""


def _e(s) -> str:
    return html.escape("" if s is None else str(s))


def build_report(case: dict, verdict: Verdict, attempts: list[dict], log_tail: list[str],
                 similar: list[dict] | None = None) -> str:
    v = verdict
    rows = []
    for s in v.suspects:
        reasons = "".join(f"<li>{_e(r)}</li>" for r in s.reasons[:4])
        ev = "".join(f"<div class='ev'><code>{_e(x)}</code></div>" for x in s.evidence[:2])
        rows.append(f"<tr><td><b>{_e(s.label)}</b><div class='ev'>{_e(s.kind)}</div></td>"
                    f"<td><div class='bar'><i style='width:{s.percent}%'></i></div>{s.percent}%</td>"
                    f"<td><ul>{reasons}</ul>{ev}</td></tr>")
    att_rows = "".join(
        f"<tr><td>{a['n']}</td><td>{_e(a.get('restore_mode'))}</td><td>{_e(a.get('outcome'))}</td>"
        f"<td>{_e(a.get('stop'))}</td><td>{_e(a.get('signatures'))}</td></tr>" for a in attempts)
    checks = "".join(f"<li>{_e(c)}</li>" for c in v.next_checks)
    warnings = "".join(f"<li>{_e(w)}</li>" for w in v.warnings)
    panic = ""
    if v.panic and not v.panic.empty:
        sensors = ", ".join(v.panic.missing_sensors) or "none listed"
        panic = (f"<h2>Panic from an earlier crash</h2><p><code>{_e(v.panic.panic_string)}</code></p>"
                 f"<p class='ev'>Missing sensors: {_e(sensors)}"
                 + (f" &middot; watchdog: {_e(v.panic.watchdog_daemon)}" if v.panic.watchdog_daemon else "")
                 + "</p>")
    sim = ""
    if similar:
        sim = "<h2>Earlier cases that stopped here</h2><ul>" + "".join(
            f"<li>{_e(r['created'])}: fixed by <b>{_e(r['fix_part'])}</b> {_e(r.get('fix_notes') or '')}</li>"
            for r in similar) + "</ul>"
    ctx = ", ".join(case.get("context") or []) or "none recorded"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Restore report</title>
<style>{CSS}</style></head><body><main>
<h1>Restore report: {_e(case.get('model_name') or case.get('product') or 'Unknown phone')}</h1>
<div class="meta">Case {_e(case.get('id'))} &middot; {_e(case.get('created'))} &middot; {_e(case.get('product'))}
 &middot; serial {_e(case.get('serial') or 'n/a')} &middot; ECID {_e(case.get('ecid') or 'n/a')}
 {('&middot; ticket ' + _e(case.get('ticket'))) if case.get('ticket') else ''}</div>
<section class="verdict {_e(v.category)}"><h2>{_e(v.headline)}</h2>
<span class="pill">{_e(v.category)}</span><span class="pill">confidence: {_e(v.confidence)}</span>
<span class="pill">{v.attempts} attempt{'s' if v.attempts != 1 else ''}</span>
{'<span class="pill">same stop repeated</span>' if v.repeated else ''}
<p>{_e(v.summary)}</p></section>
<h2>Suspects</h2><table><tr><th>Part</th><th>Weight</th><th>Why</th></tr>{''.join(rows) or '<tr><td colspan=3>None</td></tr>'}</table>
<h2>Next checks</h2><ol>{checks or '<li>None</li>'}</ol>
{('<h2>Notes</h2><ul>' + warnings + '</ul>') if warnings else ''}
{panic}{sim}
<h2>Attempts</h2><table><tr><th>#</th><th>Mode</th><th>Result</th><th>Stopped at</th><th>Signals</th></tr>{att_rows}</table>
<h2>Context</h2><p>Recent work: {_e(ctx)}. DC reading: {_e(case.get('dc_reading') or 'not measured')}.
{('Notes: ' + _e(case.get('notes'))) if case.get('notes') else ''}</p>
<details><summary>Last lines of the engine log</summary><pre>{_e(chr(10).join(log_tail))}</pre></details>
<footer>{APP_NAME} {__version__} &middot; generated {time.strftime('%Y-%m-%d %H:%M')} &middot; suspects are leads ranked
from the restore log, not a final diagnosis.</footer>
</main></body></html>"""


def write_report(folder: Path, *args, **kwargs) -> Path:
    path = Path(folder) / "report.html"
    path.write_text(build_report(*args, **kwargs), encoding="utf-8")
    return path
