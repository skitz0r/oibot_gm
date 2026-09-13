"""Render out/coverage.json + out/roster.json as a single HTML page (the buff
coverage matrix). Usage: `uv run python -m oibot_gm.report_html`."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TEMPLATE = r"""<title>25 Big Guys Buff Coverage</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&display=swap">
<style>
:root{
  --bg:#F2F4F7; --panel:#FFFFFF; --ink:#1B2230; --muted:#5B6678; --line:#D7DCE4;
  --ok:#1F9E8F; --ok-soft:#D4EFEA; --warn:#C98A1A; --warn-soft:#FBEBCB; --na:#C9CFD8;
  --barbg:#E4E8EE;
  --c-Warrior:#8A6E4B; --c-Paladin:#C2597F; --c-Hunter:#5E9B36; --c-Rogue:#B8A400; --c-Priest:#6B7280;
  --c-Shaman:#1F5FB8; --c-Mage:#2E96C2; --c-Warlock:#7B5FB8; --c-Druid:#D5701A;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --bg:#14181F; --panel:#1C222B; --ink:#E8ECF2; --muted:#98A3B5; --line:#2C3441;
  --ok:#3FC2B1; --ok-soft:#183C39; --warn:#E5B04A; --warn-soft:#3E2F12; --na:#3A4351; --barbg:#2A323D;
  --c-Warrior:#C79C6E; --c-Paladin:#F58CBA; --c-Hunter:#ABD473; --c-Rogue:#FFF569; --c-Priest:#E6E6E6;
  --c-Shaman:#5C9BFF; --c-Mage:#69CCF0; --c-Warlock:#9482C9; --c-Druid:#FF7D0A;
}}
:root[data-theme="dark"]{
  --bg:#14181F; --panel:#1C222B; --ink:#E8ECF2; --muted:#98A3B5; --line:#2C3441;
  --ok:#3FC2B1; --ok-soft:#183C39; --warn:#E5B04A; --warn-soft:#3E2F12; --na:#3A4351; --barbg:#2A323D;
  --c-Warrior:#C79C6E; --c-Paladin:#F58CBA; --c-Hunter:#ABD473; --c-Rogue:#FFF569; --c-Priest:#E6E6E6;
  --c-Shaman:#5C9BFF; --c-Mage:#69CCF0; --c-Warlock:#9482C9; --c-Druid:#FF7D0A;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 Barlow,system-ui,sans-serif;padding:0 20px;padding-block:28px 48px}
h1,h2,h3{font-family:"Barlow Condensed",Barlow,sans-serif;font-weight:600;letter-spacing:.01em;text-wrap:balance;margin:0}
h1{font-size:34px;line-height:1.05}
.sub{color:var(--muted);margin:6px 0 0;max-width:70ch}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
.legend{display:flex;flex-wrap:wrap;gap:16px;color:var(--muted);font-size:13px;align-items:center}
.legend span{display:inline-flex;align-items:center;gap:6px}
.sw{width:14px;height:14px;border-radius:3px;display:inline-block}
.sw.ok{background:var(--ok)} .sw.warn{background:var(--warn-soft);border:2px solid var(--warn)} .sw.na{background:var(--na);opacity:.5;width:8px;height:8px;border-radius:50%;margin:0 3px}
.scroll{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:6px}
table{border-collapse:separate;border-spacing:0;min-width:900px;font-variant-numeric:tabular-nums}
th,td{padding:0;border-bottom:1px solid var(--line)}
thead th{position:sticky;top:0;background:var(--panel);z-index:1;height:118px;vertical-align:bottom;font-weight:500;color:var(--muted);font-size:12px}
thead th.buff>div{writing-mode:vertical-rl;transform:rotate(180deg);padding:6px 0 6px;white-space:nowrap;margin:0 auto;width:26px;letter-spacing:.02em}
th.name,td.name{text-align:left;padding:0 12px;white-space:nowrap;min-width:200px}
td.name b{font-weight:600}
td.name small{color:var(--muted);margin-left:6px;font-size:12px}
td.cell{width:30px;height:30px;text-align:center}
td.cell i{display:block;width:16px;height:16px;margin:0 auto;border-radius:3px}
td.cell i.ok{background:var(--ok)}
td.cell i.warn{background:var(--warn-soft);border:2px solid var(--warn)}
td.cell i.na{width:6px;height:6px;border-radius:50%;background:var(--na);opacity:.6}
td.cover{padding:0 12px 0 8px;min-width:120px}
.bar{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted)}
.bar div{flex:1;height:6px;background:var(--barbg);border-radius:3px;overflow:hidden}
.bar div span{display:block;height:100%;background:var(--ok)}
tr.grp th{text-align:left;padding:10px 12px 6px;font-family:"Barlow Condensed",sans-serif;font-size:16px;font-weight:600;background:var(--bg);border-bottom:1px solid var(--line)}
tr.grp th em{font-style:normal;color:var(--muted);font-weight:500;font-size:13px;margin-left:10px}
tr.miss td{padding:6px 12px 10px;color:var(--warn);font-size:13px;background:var(--bg)}
tr.miss td b{color:var(--ink);font-weight:500}
.cls-Warrior{color:var(--c-Warrior)} .cls-Paladin{color:var(--c-Paladin)} .cls-Hunter{color:var(--c-Hunter)} .cls-Rogue{color:var(--c-Rogue)}
.cls-Priest{color:var(--c-Priest)} .cls-Shaman{color:var(--c-Shaman)} .cls-Mage{color:var(--c-Mage)} .cls-Warlock{color:var(--c-Warlock)} .cls-Druid{color:var(--c-Druid)}
.notes{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:14px}
.note{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:14px 16px}
.note h3{font-size:15px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
.note p,.note li{margin:0;font-size:14px}
.note ul{margin:0;padding-left:18px;display:flex;flex-direction:column;gap:6px}
.tag{display:inline-block;font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);border:1px solid var(--line);border-radius:3px;padding:1px 6px;margin-left:8px;vertical-align:middle}
td.cell i:focus-visible,.scroll:focus-visible{outline:2px solid var(--ok);outline-offset:2px}
@media (max-width:640px){h1{font-size:28px} th.name,td.name{min-width:150px}}
</style>
<div class="wrap">
  <header>
    <h1>Buff coverage — __TITLE__</h1>
    <p class="sub">__SUBTITLE__</p>
  </header>
  <div class="legend">
    <span><i class="sw ok"></i> benefits, covered in group</span>
    <span><i class="sw warn"></i> benefits, nobody in group provides it</span>
    <span><i class="sw na"></i> not relevant to this spec</span>
    <span>Names in class colour · hover a cell for the provider</span>
  </div>
  <div class="scroll" tabindex="0"><table id="grid"></table></div>
  <section class="notes" id="notes"></section>
</div>
<script>
const COV = __COVERAGE__;
const ROSTER = __ROSTER__;
const short = n => n.split(' (')[0].replace(' Totem','').replace(' / ','/');
const grid = document.getElementById('grid');
let h = '<thead><tr><th class="name">player</th>' + COV.buffs.map(b=>`<th class="buff" title="${b}"><div>${short(b)}</div></th>`).join('') + '<th class="cover">coverage</th></tr></thead><tbody>';
const groupVal = i => (ROSTER.group_reports[i]||{}).value;
COV.groups.forEach((g,gi)=>{
  h += `<tr class="grp"><th colspan="${COV.buffs.length+2}">Group ${g.index}<em>synergy +${groupVal(gi)}</em></th></tr>`;
  g.players.forEach(p=>{
    const tot=p.covered+p.missing, pct= tot? Math.round(100*p.covered/tot):100;
    h += `<tr><td class="name"><b class="cls-${p.cls}">${p.name}</b><small>${p.spec} · ${p.role}</small></td>`;
    p.cells.forEach(c=>{
      const state = c.value<=0 ? 'na' : (c.present?'ok':'warn');
      const tip = c.value<=0 ? `${c.buff}: n/a` : (c.present? `${c.buff}: +${c.value} from ${c.providers.join(', ')}` : `${c.buff}: missing (+${c.value} if provided)`);
      h += `<td class="cell" title="${tip}"><i class="${state}" tabindex="0" aria-label="${tip}"></i></td>`;
    });
    h += `<td class="cover"><div class="bar"><div><span style="width:${pct}%"></span></div>${pct}%</div></td></tr>`;
  });
  if (g.missing_summary.length) h += `<tr class="miss"><td colspan="${COV.buffs.length+2}"><b>Missing:</b> ${g.missing_summary.join(' · ')}</td></tr>`;
});
grid.innerHTML = h + '</tbody>';
const notes = document.getElementById('notes');
let n = '';
if (COV.unmet_raidwide.length) n += `<div class="note"><h3>Nobody on the roster provides</h3><p>${COV.unmet_raidwide.join(', ')}</p></div>`;
n += `<div class="note"><h3>Benched</h3><ul>${ROSTER.benched.map(p=>`<li><span class="cls-${p.cls}">${p.character||p.signup_name}</span> ${p.spec} <span class="tag">${p.status}</span></li>`).join('')||'<li>nobody</li>'}</ul></div>`;
n += `<div class="note"><h3>Advisories</h3><ul>${ROSTER.advisories.map(a=>`<li>${a}</li>`).join('')}</ul></div>`;
notes.innerHTML = n;
</script>
"""


def render(out: Path = ROOT / "out", title: str = "", subtitle: str = "") -> Path:
    cov = (out / "coverage.json").read_text()
    roster = (out / "roster.json").read_text()
    html = (
        TEMPLATE.replace("__COVERAGE__", cov)
        .replace("__ROSTER__", roster)
        .replace("__TITLE__", title)
        .replace("__SUBTITLE__", subtitle)
    )
    path = out / "coverage.html"
    path.write_text(html)
    return path


if __name__ == "__main__":
    import sys

    p = render(title=sys.argv[1] if len(sys.argv) > 1 else "roster", subtitle=sys.argv[2] if len(sys.argv) > 2 else "")
    print(p)
