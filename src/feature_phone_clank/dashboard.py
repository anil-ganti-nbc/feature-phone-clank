"""Read-only local Feature Phone field-test dashboard."""
from __future__ import annotations
import html, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .paths import resolve_data_path
from .providers.sqlite import SqliteStore
from .runtime_bridge import get_version_info

def e(x): return html.escape("" if x is None else str(x))
def a(url): return f'<a href="{e(url)}" target=_blank rel=noreferrer>Source ↗</a>' if url else '—'
def table(h, rows, title, detail):
    if not rows: return f'<div class=empty><b>{title}</b><span>{detail}</span></div>'
    return '<div class=scroll><table><thead><tr>'+''.join(f'<th>{e(x)}</th>' for x in h)+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{x}</td>' for x in r)+'</tr>' for r in rows)+'</tbody></table></div>'
def render(db):
    store=SqliteStore(str(db))
    try:
        classes={c:store.classification_log('hmd-nokia',c) for c in ('feature_phone','ambiguous','smartphone')}
        incomplete=store.incomplete_spec_products('hmd-nokia'); events=store.recent_events(limit=20); runs=store.recent_runs(20)
        products=store.db.execute("SELECT p.*,o.spec_completeness FROM products p LEFT JOIN observations o ON o.id=(SELECT id FROM observations WHERE product_id=p.id ORDER BY id DESC LIMIT 1) ORDER BY p.last_seen_at DESC").fetchall()
    finally: store.close()
    vi=get_version_info(); rev='local development build' if vi['source_revision_short']=='unknown' else vi['source_revision_short']
    accepted=[(e(x['slug']),e(x['last_seen_at']),a(x['url'])) for x in classes['feature_phone']]
    amb=[(e(x['slug']),e(x['last_seen_at']),e(x['evidence_json']),a(x['url'])) for x in classes['ambiguous']]
    rejected=[(e(x['slug']),e(x['last_seen_at']),e(x['evidence_json']),a(x['url'])) for x in classes['smartphone']]
    inc=[(e(x['model'] or x['product_key']),e(x['spec_completeness']),e(x['observed_at']),a(x['url'])) for x in incomplete]
    ev=[(e(x['model'] or x['product_key']),e(x['event_type']),e(x['detected_at']),a(x['url'])) for x in events]
    runrows=[(e(x['source_key']),e(x['started_at']),e(x['status']),e(x['products_observed'] or '—')) for x in runs]
    productrows=[(e(x['model']),e(x['model_number'] or '—'),e(x['status']),e(x['spec_completeness'] or '—'),e(x['last_seen_at']),a(x['url'])) for x in products]
    css='''<style>:root{--bg:#08111d;--nav:#0c1727;--card:#111f31;--line:#26374d;--text:#e9eef7;--muted:#9baac0;--blue:#67aeff;--green:#62dd89;--amber:#f7bd48}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.42 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:#79c0ff;text-decoration:none}.app{min-height:100vh;display:grid;grid-template-columns:215px 1fr;grid-template-rows:72px 1fr}header{grid-column:1/3;display:flex;align-items:center;gap:16px;padding:0 22px;background:#0b1524;border-bottom:1px solid var(--line)}.brand{font-size:17px;font-weight:750}.brand small,.muted,small{display:block;color:var(--muted);font-size:11px;font-weight:400}.pill{color:#d1baff;background:#25235c;border:1px solid #4842a2;border-radius:5px;padding:5px 8px;font-size:10px;font-weight:750}.prov{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:460px}.spacer{flex:1}button{background:#30358e;color:#fff;border:0;border-radius:6px;padding:9px 13px;font-weight:700}aside{background:var(--nav);border-right:1px solid var(--line);padding:13px}.navtitle{font-size:10px;color:var(--muted);letter-spacing:.07em;margin:14px 8px 5px}.nav{display:block;color:#d6dfed;padding:8px 10px;border-radius:5px;margin:2px 0}.nav.active{background:#302d80;color:#fff;font-weight:700}main{max-width:1600px;width:100%;padding:18px 22px;margin:auto}.summary,.three{display:grid;grid-template-columns:repeat(5,1fr);gap:11px}.metric,.card{background:var(--card);border:1px solid var(--line);border-radius:8px}.metric{padding:14px;min-height:102px}.label{font-size:10px;color:var(--muted);letter-spacing:.05em;font-weight:750}.number{font-size:28px;font-weight:780;margin:4px 0}.health{border-color:#916a26;background:#231f16}.health .number{font-size:20px;color:var(--amber)}.grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(260px,1fr);gap:14px;margin-top:14px}.three{grid-template-columns:repeat(3,1fr);margin-top:14px}.card{padding:14px}h2{font-size:15px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;color:var(--muted);font-size:10px;letter-spacing:.05em;padding:8px 7px;border-bottom:1px solid var(--line)}td{padding:9px 7px;border-bottom:1px solid #213047;vertical-align:top}.empty{min-height:105px;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:5px;text-align:center;color:var(--muted)}.empty b{color:#dce5f2}.guide{margin-top:14px;background:#142237;border:1px solid var(--line);border-radius:8px;padding:12px;color:var(--muted)}@media(max-width:1000px){.app{grid-template-columns:1fr;grid-template-rows:72px auto 1fr}header{grid-column:1}aside{display:flex;overflow:auto;border-right:0;border-bottom:1px solid var(--line);padding:7px}.navtitle{display:none}.nav{white-space:nowrap}.summary,.grid,.three{grid-template-columns:1fr}}</style>'''
    page = f'''<!doctype html><html><head><meta charset=utf-8>{css}<title>Feature Phone Clank</title></head><body><div class=app><header><div class=brand>☎ Feature Phone Clank<small>HMD / Nokia classification intelligence</small></div><span class=pill>FIELD TEST MODE</span><span class=prov title="{e(str(db))}">Revision: {e(rev)} | Database: {e(db.name)}</span><span class=spacer></span><button onclick="location.reload()">↻ Refresh</button></header><aside><a class="nav active" href=#overview>⌂ Overview</a><div class=navtitle>CLASSIFICATION</div><a class=nav href=#accepted>✓ Accepted</a><a class=nav href=#ambiguous>? Ambiguous</a><a class=nav href=#rejected>⊘ Rejected Smartphones</a><a class=nav href=#incomplete>◫ Incomplete</a><div class=navtitle>CHANGES</div><a class=nav href=#events>◉ Recent Events</a><a class=nav href=#products>⌕ Product Details</a><div class=navtitle>SYSTEM</div><a class=nav href=#runs>◷ Run History</a><a class=nav href=#about>ⓘ About</a></aside><main id=overview><section class=summary><div class="metric health"><div class=label>OVERALL HEALTH</div><div class=number>{'WARNING' if not runs else 'HEALTHY'}</div><span class=muted>Field-test local state</span></div><div class=metric><div class=label>ACCEPTED</div><div class=number>{len(accepted)}</div><span class=muted>Feature phones</span></div><div class=metric><div class=label>AMBIGUOUS</div><div class=number>{len(amb)}</div><span class=muted>Needs owner review</span></div><div class=metric><div class=label>REJECTED SMARTPHONES</div><div class=number>{len(rejected)}</div><span class=muted>Expected classifier outcome</span></div><div class=metric><div class=label>INCOMPLETE</div><div class=number>{len(inc)}</div><span class=muted>Present, incomplete specs</span></div></section><section class=grid><div class=card><h2>Classification Overview</h2>{table(('Product','Latest observed','Source'),accepted,'No accepted feature phones','Accepted HMD/Nokia feature phones will appear here.')}</div><div class=card><h2>Read-only field test</h2><p class=muted>Inspect classifier decisions, identity evidence, and events. This UI has no collector or database mutation controls.</p><a href=#runs>View Run History →</a><br><a href=#events>View Recent Events →</a></div></section><section class=three><div class=card id=ambiguous><h2>Ambiguous Candidates</h2>{table(('Candidate','Last observed','Stored evidence','Source'),amb,'No ambiguous candidates','No currently ambiguous products are recorded.')}</div><div class=card id=rejected><h2>Rejected Smartphones</h2>{table(('Candidate','Last observed','Stored evidence','Source'),rejected,'No rejected smartphones','Expected classifier rejections will appear here.')}</div><div class=card id=incomplete><h2>Incomplete Products</h2>{table(('Product','Completeness','Observed','Source'),inc,'No incomplete products','Incomplete specs do not mean a product disappeared.')}</div></section><section class=card id=events style="margin-top:14px"><h2>Recent Events / Changes</h2>{table(('Product','Event','Timestamp','Evidence'),ev,'No recent events','New product and change events will appear here.')}</section><section class=card id=products style="margin-top:14px"><h2>Product Detail / Identity Evidence</h2>{table(('Resolved product','Model','Presence','Completeness','Latest observation','Source'),productrows,'No products recorded','Product identity and evidence will appear here after local field-test data is present.')}</section><section class=card id=runs style="margin-top:14px"><h2>Run History</h2>{table(('Source','Started','Status','Observed'),runrows,'No runs recorded yet','Scheduled or local field-test runs will appear here.')}</section><section class=guide id=about><b>Identity safety guidance.</b> Recycled Nokia/HMD names can represent different generations: rely on model and source evidence, not marketing name alone. Incomplete specs do not equal disappearance. Rejected smartphones are expected classifier behaviour, not errors.</section></main></div></body></html>'''
    return page
def serve(host='127.0.0.1',port=8400):
    db=resolve_data_path('data/feature_phone_clank.db')
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/healthz':
                body=json.dumps({'status':'ok','database':str(db)}).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)
                return
            if path != '/':
                self.send_error(404)
                return
            body = render(db).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body)
        def log_message(self,*_): pass
    return ThreadingHTTPServer((host,port),H)
