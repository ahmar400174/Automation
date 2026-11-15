"""
report_builder.py
Builds the interactive accordion HTML report (parent = competitor page, child = ads).

Input: ONE flat DataFrame, one row per ad, with (rename via `columns=` if yours differ):
  page_id, page_name, page_profile (img url, optional), site_url,
  library_id, ad_preview (img url, optional), page_url, launched (date-like),
  ad_count (optional: number of ads sharing that library id -> "N ads" badge)
"""
from __future__ import annotations
import html
from datetime import date
import pandas as pd

DEFAULT_COLUMNS = dict(
    page_id="page_id", page_name="page_name", page_profile="page_profile",
    site_url="site_url", library_id="library_id", ad_preview="ad_preview",
    page_url="page_url", launched="launched", ad_count="ad_count",
)

CSS = """
:root{--bd:#e3e6ea;--bg:#f6f7f9;--tx:#1c1e21;--mut:#6b7280;--lnk:#1d5fd1}
@media(prefers-color-scheme:dark){:root{--bd:#2f3640;--bg:#14171c;--tx:#e8eaed;--mut:#9aa3af;--lnk:#6ea8ff}}
*{box-sizing:border-box}body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;padding:20px;background:var(--bg);color:var(--tx)}
h1{font-size:22px;margin:0 0 14px}
.bar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:14px}
.btn,select,input{font:inherit;padding:8px 14px;border:1px solid var(--bd);border-radius:8px;background:transparent;color:var(--tx)}
.btn{cursor:pointer}.btn:hover{border-color:var(--lnk)}.btn.on{background:#111;color:#fff;border-color:#111}
.mut{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%}
.main{background:rgba(127,127,127,.06);border:1px solid var(--bd);border-radius:10px;overflow:hidden}
.main>thead th{text-align:left;padding:10px 14px;border-bottom:1px solid var(--bd);font-size:13px;color:var(--mut)}
tr.parent{cursor:pointer;border-top:1px solid var(--bd)}tr.parent:hover{background:rgba(127,127,127,.12)}
tr.parent td{padding:12px 14px;vertical-align:middle}
.toggle{display:inline-block;width:30px;height:30px;line-height:28px;text-align:center;border:1px solid var(--bd);border-radius:6px;transition:transform .2s}
tr.parent.open .toggle{transform:rotate(180deg)}
.pf{width:56px;height:56px;border-radius:8px;object-fit:cover;background:var(--bd)}
.pname{font-size:18px;font-weight:600;color:var(--lnk)}.site{font-size:17px;font-weight:600;color:var(--lnk);text-decoration:none}
.total{font-size:20px;font-weight:700;text-align:center}
tr.child{display:none}tr.child.open{display:table-row}
tr.child>td{padding:0 0 0 46px;background:rgba(127,127,127,.05)}
.wrap{animation:slide .22s ease-out;overflow:hidden}
@keyframes slide{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.sub th{text-align:left;font-size:13px;color:var(--lnk);padding:10px 12px;border-bottom:1px solid var(--bd)}
.sub td{padding:9px 12px;border-top:1px solid var(--bd);vertical-align:middle;font-size:14px}
.sub tr:hover td{background:rgba(127,127,127,.1)}
.thumb{width:72px;height:72px;border-radius:6px;object-fit:cover;background:var(--bd);display:flex;align-items:center;justify-content:center;color:var(--mut);font-size:11px}
a{color:var(--lnk);text-decoration:none}a:hover{text-decoration:underline}
.pill{display:inline-block;margin-left:8px;padding:2px 9px;border-radius:99px;background:#1d5fd1;color:#fff;font-size:12px;font-weight:600}
.badge{display:inline-block;padding:3px 10px;border-radius:99px;font-size:12px;font-weight:700;margin-left:8px}
.b-new{background:#d9f5e1;color:#13692f}.b-win{background:#dbe9ff;color:#1747a6}.b-old{background:#f3e0e0;color:#8c1d1d}
.days{font-weight:700}.d-new{color:#13892f}.d-win{color:#1d5fd1}.d-old{color:#c0392b}
.empty{padding:30px;text-align:center;color:var(--mut)}
"""

JS = """
const PAGES=[...document.querySelectorAll('tr.parent')];
let seg='all',q='',sortMode='ads';
function inSeg(d){return seg==='all'||(seg==='new'&&d>=0&&d<=4)||(seg==='win'&&d>=5&&d<=12)||(seg==='old'&&d>12)}
function toggle(p){p.classList.toggle('open');p.nextElementSibling.classList.toggle('open')}
PAGES.forEach(p=>p.addEventListener('click',e=>{if(e.target.closest('a'))return;toggle(p)}));
function apply(){
  let shown=0,ads=0;
  PAGES.forEach(p=>{
    const c=p.nextElementSibling,rows=[...c.querySelectorAll('tr.ad')];let vis=0;
    rows.forEach(r=>{const ok=inSeg(+r.dataset.days);r.style.display=ok?'':'none';if(ok)vis++});
    const match=!q||p.dataset.search.includes(q);
    const show=match&&vis>0;
    p.style.display=show?'':'none';
    if(!show){c.classList.remove('open');p.classList.remove('open')}
    p.querySelector('.total').textContent=seg==='all'?p.dataset.total:vis;
    p.dataset.vis=vis;if(show){shown++;ads+=vis}
  });
  const tb=document.getElementById('tbody');
  [...PAGES].sort((a,b)=>sortMode==='ads'?(+b.dataset.vis)-(+a.dataset.vis):a.dataset.name.localeCompare(b.dataset.name))
    .forEach(p=>{tb.appendChild(p);tb.appendChild(p.nextElementSibling)});
  document.getElementById('stat').textContent=shown+' competitors · '+ads+' ads';
  document.getElementById('empty').style.display=shown?'none':'block';
}
document.querySelectorAll('[data-seg]').forEach(b=>b.addEventListener('click',()=>{
  seg=b.dataset.seg;document.querySelectorAll('[data-seg]').forEach(x=>x.classList.toggle('on',x===b));apply()}));
document.getElementById('q').addEventListener('input',e=>{q=e.target.value.toLowerCase().trim();apply()});
document.getElementById('sort').addEventListener('change',e=>{sortMode=e.target.value;apply()});
document.getElementById('expand').addEventListener('click',()=>{
  const open=PAGES.some(p=>p.style.display!=='none'&&!p.classList.contains('open'));
  PAGES.forEach(p=>{if(p.style.display==='none')return;
    p.classList.toggle('open',open);p.nextElementSibling.classList.toggle('open',open)})});
apply();
"""


def _e(v) -> str:
    return html.escape("" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v), quote=True)


def _safe_url(u) -> str:
    u = _e(u).strip()
    if not u:
        return ""
    return u if u.lower().startswith(("http://", "https://")) else "https://" + u


def classify(days: int) -> tuple[str, str, str]:
    """-> (css key, badge label, badge class)"""
    if days <= 4:
        return "new", "New test", "b-new"
    if days <= 12:
        return "win", "Winning", "b-win"
    return "old", "Scaling/Old", "b-old"


def prepare(df: pd.DataFrame, columns: dict | None = None, today: date | None = None) -> pd.DataFrame:
    c = {**DEFAULT_COLUMNS, **(columns or {})}
    d = df.rename(columns={v: k for k, v in c.items() if v in df.columns}).copy()
    for k in DEFAULT_COLUMNS:
        if k not in d:
            d[k] = None
    # Days_Live = today - launch date, computed on whole calendar days (no time-of-day drift)
    launched = pd.to_datetime(d["launched"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    now = pd.Timestamp(today or date.today()).normalize()
    d["launched_dt"] = launched
    d["Days_Live"] = (now - launched).dt.days
    d = d.dropna(subset=["Days_Live"])
    d["Days_Live"] = d["Days_Live"].clip(lower=0).astype(int)
    d["ad_count"] = pd.to_numeric(d["ad_count"], errors="coerce").fillna(1).astype(int)
    return d


def build_html_report(df: pd.DataFrame, out_path: str, title: str = "Competitors report",
                      columns: dict | None = None, today: date | None = None) -> str:
    d = prepare(df, columns, today)
    parts = []
    groups = d.groupby(d["page_id"].fillna(d["page_name"]), sort=False)
    ordered = sorted(groups, key=lambda kv: -len(kv[1]))
    for _, g in ordered:
        g = g.sort_values("Days_Live", ascending=True)
        first = g.iloc[0]
        total = len(g)
        site = _safe_url(first["site_url"])
        site_txt = _e(str(first["site_url"] or "").replace("https://", "").replace("http://", "").rstrip("/"))
        prof = _safe_url(first["page_profile"])
        prof_html = f'<img class="pf" loading="lazy" src="{prof}" alt="">' if prof else '<div class="pf"></div>'
        search = _e(f'{first["page_name"]} {first["site_url"]}'.lower())
        rows = []
        for _, r in g.iterrows():
            key, label, bcls = classify(int(r["Days_Live"]))
            prev = _safe_url(r["ad_preview"])
            prev_html = (f'<img class="thumb" loading="lazy" src="{prev}" alt="">' if prev
                         else '<div class="thumb">no preview</div>')
            lid = _e(r["library_id"])
            lib = f'https://www.facebook.com/ads/library/?id={lid}'
            pill = f'<span class="pill">{int(r["ad_count"])} ads</span>' if r["ad_count"] > 1 else ""
            pu = _safe_url(r["page_url"])
            pu_html = f'<a href="{pu}" target="_blank" rel="noopener">{_e(r["page_url"])}</a>' if pu else '<span class="mut">(no link read)</span>'
            rows.append(
                f'<tr class="ad" data-days="{int(r["Days_Live"])}"><td>{prev_html}</td>'
                f'<td><a href="{lib}" target="_blank" rel="noopener">Library ID {lid}</a>{pill}</td>'
                f'<td>{pu_html}</td><td>{r["launched_dt"].strftime("%Y-%m-%d")}</td>'
                f'<td><span class="days d-{key}">{int(r["Days_Live"])} d</span>'
                f'<span class="badge {bcls}">{label}</span></td></tr>')
        parts.append(
            f'<tr class="parent" data-total="{total}" data-name="{_e(first["page_name"])}" data-search="{search}">'
            f'<td style="width:60px"><span class="toggle">▾</span></td><td style="width:90px">{prof_html}</td>'
            f'<td><div class="pname">{_e(first["page_name"])}</div></td>'
            f'<td>{f"<a class=site href={chr(34)}{site}{chr(34)} target=_blank rel=noopener>{site_txt}</a>" if site else ""}</td>'
            f'<td class="total">{total}</td></tr>'
            f'<tr class="child"><td colspan="5"><div class="wrap"><table class="sub"><thead><tr>'
            f'<th>Ad preview</th><th>Ad link</th><th>Page URL</th><th>Launched</th><th>Days live</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></td></tr>')

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{_e(title)}</title>
<style>{CSS}</style></head><body>
<h1>{_e(title)}</h1>
<div class="bar">
 <button class="btn on" data-seg="all">All</button>
 <button class="btn" data-seg="new">New Tests (1-4 d)</button>
 <button class="btn" data-seg="win">Winning Trajectory (5-12 d)</button>
 <button class="btn" data-seg="old">Scaling/Old (&gt;12 d)</button>
 <input id="q" placeholder="Search a name or site" size="26">
 <select id="sort"><option value="ads">Most active ads first</option><option value="name">Name A-Z</option></select>
 <button class="btn" id="expand">Expand / collapse all</button>
 <span class="mut" id="stat"></span>
</div>
<table class="main"><thead><tr><th></th><th>Profile</th><th>Page</th><th>Site</th><th style="text-align:center">Active ads</th></tr></thead>
<tbody id="tbody">{"".join(parts)}</tbody></table>
<div class="empty" id="empty" style="display:none">No ads match this filter.</div>
<script>{JS}</script></body></html>"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_path


if __name__ == "__main__":
    demo = pd.DataFrame([
        dict(page_id=1, page_name="Cuurt Community", site_url="cuurt.com", library_id="1355346263453593", ad_count=4,
             page_url="cuurt.com/pages/landing-page-cuurt-muuv-shoes", launched="2026-07-27"),
        dict(page_id=1, page_name="Cuurt Community", site_url="cuurt.com", library_id="1035055239329321",
             page_url=None, launched="2026-10-05"),
        dict(page_id=1, page_name="Cuurt Community", site_url="cuurt.com", library_id="1740580170726143",
             page_url="cuurt.com/pages/x", launched="2026-11-03"),
    ])
    print(build_html_report(demo, "demo_report.html", today=date(2026, 11, 7)))
