"""Local Feature Phone field-test dashboard.

Phase 0 (original): unconditionally read-only, every POST 403s -- "no
authenticated mutation profile exists". Phase 1 (2026-08-27, GUI/QC parity
pass with Watch Clank -- see docs/FEATURE_PHONE_SCOPE_EXPANSION.md and
watch-clank/app/local_operator.py for the reference design this mirrors):
adds a narrow, explicit LOCAL-OPERATOR mutation surface alongside the
untouched legacy Phase 0 surface:

  - the legacy `/api/local-collection/run` path still always 403s,
    unconditionally, exactly as before (nothing here removes that
    contract, and its own tests keep passing unchanged);
  - a new, separate, explicitly-allowlisted set of routes
    (`/operations/run/<source_key>`, `/operations/run-experimental/<source_key>`,
    `/operations/run-all`, `/api/qc/review/<event_id>`) is authorized ONLY
    when: (a) the server was started with a real `LocalCollectionController`
    (never for `controller=None` or a bare placeholder object -- the exact
    same duck the old Phase 0 tests already exercise), AND (b) the request
    arrives from a loopback client address to a loopback Host header,
    matching Watch Clank's own `request_is_local_operator_mutation` check
    byte-for-byte in spirit. Everything else -- including any future POST
    -- stays 403/404.

"Run all collectors" only ever runs `config/scope.yaml`'s
`production_collectors` (today: hmd-nokia). Every other registered
collector (itel-india, lava-india, punkt-ch, doro-gb, mudita-com,
sunbeam-f1-us, tcl-alcatel-global -- see collectors/__init__.py) is
experimental/soak: it is listed on the dashboard as an individually
runnable, clearly-labelled "Experimental / Soak" control that writes only
to the isolated experimental database, and it is NEVER included in "Run
all" merely because it is registered. Promoting one out of soak is a
one-line edit to config/scope.yaml, never a dashboard action.

QC contract (modeled on Watch Clank's EventReview -- see
providers/qc_store.py's module docstring for the full contract): a
decision (USEFUL / NOT_USEFUL / FALSE_POSITIVE / OUT_OF_STOCK) on an event
transactionally archives that event's full evidence + provenance into a
wholly separate `feature_phone_clank_qc.db`, removes it from the default
Recent Events view immediately, and appears in the "Recently QCed" tab.
Nothing is ever deleted from the production database.
"""
from __future__ import annotations
import html, ipaddress, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from .paths import resolve_data_path
from .providers.sqlite import SqliteStore
from .providers.qc_store import DECISIONS, QcArchiveStore
from .runtime_bridge import get_version_info

DEFAULT_QC_DB = "data/feature_phone_clank_qc.db"

def e(x): return html.escape("" if x is None else str(x))
def a(url): return f'<a href="{e(url)}" target=_blank rel=noreferrer>Source ↗</a>' if url else '—'
def table(h, rows, title, detail):
    if not rows: return f'<div class=empty><b>{title}</b><span>{detail}</span></div>'
    return '<div class=scroll><table><thead><tr>'+''.join(f'<th>{e(x)}</th>' for x in h)+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{x}</td>' for x in r)+'</tr>' for r in rows)+'</tbody></table></div>'


def _loopback(value):
    if not value:
        return False
    value = value.strip().strip("[]")
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def _registered_collectors():
    """(production_keys, experimental_keys) -- every registered collector,
    split by config/scope.yaml membership. Import is local so importing
    dashboard.py never has a side effect on the collector registry."""
    from . import collectors as _collectors  # noqa: F401 — registration side effect
    from .core.registry import collectors
    from .core.scope import load_scope
    from .paths import resolve_config_path

    scope = load_scope(resolve_config_path("scope.yaml"))
    all_names = collectors.names()
    production = [n for n in all_names if n in scope.production_collectors]
    experimental = [n for n in all_names if n not in scope.production_collectors]
    return production, experimental


def _qc_action_buttons(event_id):
    btns = "".join(
        f'<button class=qcbtn data-event="{event_id}" data-decision="{d}" onclick="qcReview(this)">{d.replace("_"," ").title()}</button>'
        for d in sorted(DECISIONS)
    )
    return f'<div class=qcrow>{btns}</div>'


def render(db, controller=None, qc_db=None):
    from .local_collection import LocalCollectionController

    qc_db = qc_db or resolve_data_path(DEFAULT_QC_DB)
    store=SqliteStore(str(db))
    qc_store = QcArchiveStore(str(qc_db))
    try:
        classes={c:store.classification_log('hmd-nokia',c) for c in ('feature_phone','ambiguous','smartphone')}
        incomplete=store.incomplete_spec_products('hmd-nokia')
        all_events=store.recent_events(limit=50)
        runs=store.recent_runs(20)
        products=store.db.execute("SELECT p.*,o.spec_completeness FROM products p LEFT JOIN observations o ON o.id=(SELECT id FROM observations WHERE product_id=p.id ORDER BY id DESC LIMIT 1) ORDER BY p.last_seen_at DESC").fetchall()
        reviewed_ids = qc_store.reviewed_event_ids()
        recent_qc = qc_store.recent_reviews(limit=30)
    finally:
        store.close()
        qc_store.close()
    is_operator = isinstance(controller, LocalCollectionController)
    try:
        production_keys, experimental_keys = _registered_collectors()
    except Exception:
        production_keys, experimental_keys = [], []
    controller_snapshot = controller.snapshot() if is_operator else {}
    # Sources health (Watch Clank dashboard parity): healthy/degraded/never-run,
    # computed from this database's own collector_runs -- production
    # collectors only, since experimental/soak collectors deliberately never
    # write here (they have their own isolated experimental database, see
    # docs/FEATURE_PHONE_SCOPE_EXPANSION.md section 10).
    _last_status_by_source = {}
    for r in runs:
        _last_status_by_source.setdefault(r["source_key"], r["status"])
    healthy_sources = [k for k in production_keys if _last_status_by_source.get(k) == "ok"]
    degraded_sources = [k for k in production_keys if k in _last_status_by_source and _last_status_by_source[k] != "ok"]
    never_run_sources = [k for k in production_keys if k not in _last_status_by_source]
    vi=get_version_info(); rev='local development build' if vi['source_revision_short']=='unknown' else vi['source_revision_short']
    accepted=[(e(x['slug']),e(x['last_seen_at']),a(x['url'])) for x in classes['feature_phone']]
    amb=[(e(x['slug']),e(x['last_seen_at']),e(x['evidence_json']),a(x['url'])) for x in classes['ambiguous']]
    rejected=[(e(x['slug']),e(x['last_seen_at']),e(x['evidence_json']),a(x['url'])) for x in classes['smartphone']]
    inc=[(e(x['model'] or x['product_key']),e(x['spec_completeness']),e(x['observed_at']),a(x['url'])) for x in incomplete]
    active_events=[x for x in all_events if x['id'] not in reviewed_ids]
    ev=[(e(x['model'] or x['product_key']),e(x['event_type']),e(x['detected_at']),a(x['url']),(_qc_action_buttons(x['id']) if is_operator else '—')) for x in active_events]
    runrows=[(e(x['source_key']),e(x['started_at']),e(x['status']),e(x['products_observed'] or '—')) for x in runs]
    productrows=[(e(x['model']),e(x['model_number'] or '—'),e(x['status']),e(x['spec_completeness'] or '—'),e(x['last_seen_at']),a(x['url'])) for x in products]
    qcrows=[(e(x['model'] or x['product_key']),e(x['source_key']),e(x['event_type']),e(x['decision']),e(x['decided_at']),('corrected' if x['is_corrected'] else '—'),a(x['url'])) for x in recent_qc]
    css='''<style>:root{--bg:#08111d;--nav:#0c1727;--card:#111f31;--line:#26374d;--text:#e9eef7;--muted:#9baac0;--blue:#67aeff;--green:#62dd89;--amber:#f7bd48}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.42 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:#79c0ff;text-decoration:none}.app{min-height:100vh;display:grid;grid-template-columns:215px 1fr;grid-template-rows:72px 1fr}header{grid-column:1/3;display:flex;align-items:center;gap:16px;padding:0 22px;background:#0b1524;border-bottom:1px solid var(--line)}.brand{font-size:17px;font-weight:750}.brand small,.muted,small{display:block;color:var(--muted);font-size:11px;font-weight:400}.pill{color:#d1baff;background:#25235c;border:1px solid #4842a2;border-radius:5px;padding:5px 8px;font-size:10px;font-weight:750}.prov{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:460px}.spacer{flex:1}button{background:#30358e;color:#fff;border:0;border-radius:6px;padding:9px 13px;font-weight:700;cursor:pointer}button:disabled{opacity:.45;cursor:default}aside{background:var(--nav);border-right:1px solid var(--line);padding:13px}.navtitle{font-size:10px;color:var(--muted);letter-spacing:.07em;margin:14px 8px 5px}.nav{display:block;color:#d6dfed;padding:8px 10px;border-radius:5px;margin:2px 0}.nav.active{background:#302d80;color:#fff;font-weight:700}main{max-width:1600px;width:100%;padding:18px 22px;margin:auto}.summary,.three{display:grid;grid-template-columns:repeat(5,1fr);gap:11px}.metric,.card{background:var(--card);border:1px solid var(--line);border-radius:8px}.metric{padding:14px;min-height:102px}.label{font-size:10px;color:var(--muted);letter-spacing:.05em;font-weight:750}.number{font-size:28px;font-weight:780;margin:4px 0}.health{border-color:#916a26;background:#231f16}.health .number{font-size:20px;color:var(--amber)}.grid{display:grid;grid-template-columns:minmax(0,3fr) minmax(260px,1fr);gap:14px;margin-top:14px}.three{grid-template-columns:repeat(3,1fr);margin-top:14px}.card{padding:14px}h2{font-size:15px;margin:0 0 12px}table{width:100%;border-collapse:collapse;font-size:12px}th{text-align:left;color:var(--muted);font-size:10px;letter-spacing:.05em;padding:8px 7px;border-bottom:1px solid var(--line)}td{padding:9px 7px;border-bottom:1px solid #213047;vertical-align:top}.empty{min-height:105px;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:5px;text-align:center;color:var(--muted)}.empty b{color:#dce5f2}.guide{margin-top:14px;background:#142237;border:1px solid var(--line);border-radius:8px;padding:12px;color:var(--muted)}.collectorrow{display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid #213047}.collectorrow:last-child{border-bottom:0}.collectorrow .name{flex:1}.badge{font-size:10px;padding:3px 7px;border-radius:5px;font-weight:750}.badge-ok{background:#123322;color:var(--green)}.badge-warn{background:#3a2c12;color:var(--amber)}.badge-muted{background:#1c2636;color:var(--muted)}.badge-soak{background:#3a1f3a;color:#e39be0}.smallbtn{padding:6px 10px;font-size:11px}.qcrow{display:flex;gap:4px;flex-wrap:wrap}.qcbtn{background:#1c2c46;padding:5px 8px;font-size:10px;font-weight:600}.qcbtn:hover{background:#30358e}@media(max-width:1000px){.app{grid-template-columns:1fr;grid-template-rows:72px auto 1fr}header{grid-column:1}aside{display:flex;overflow:auto;border-right:0;border-bottom:1px solid var(--line);padding:7px}.navtitle{display:none}.nav{white-space:nowrap}.summary,.grid,.three{grid-template-columns:1fr}}</style>'''
    js = '''<script>
async function qcReview(btn){
  const eventId=btn.dataset.event, decision=btn.dataset.decision;
  btn.closest('.qcrow').querySelectorAll('button').forEach(b=>b.disabled=true);
  try{
    const resp=await fetch(`/api/qc/review/${eventId}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision})});
    if(resp.ok){ btn.closest('tr').style.opacity='0.35'; btn.closest('tr').querySelector('.qcrow').outerHTML='<span class=muted>QC recorded — refresh to update lists</span>'; }
    else { alert('QC submit failed: '+resp.status); btn.closest('.qcrow').querySelectorAll('button').forEach(b=>b.disabled=false); }
  }catch(err){ alert('QC submit error: '+err); btn.closest('.qcrow').querySelectorAll('button').forEach(b=>b.disabled=false); }
}
async function runCollector(sourceKey, mode){
  const path = mode==='experimental' ? `/operations/run-experimental/${sourceKey}` : `/operations/run/${sourceKey}`;
  const resp = await fetch(path, {method:'POST'});
  const body = await resp.json().catch(()=>({}));
  alert(resp.ok ? `Queued: ${sourceKey}` : `Could not start ${sourceKey}: ${body.error||resp.status}`);
  setTimeout(()=>location.reload(), 600);
}
async function runAll(){
  const resp = await fetch('/operations/run-all', {method:'POST'});
  const body = await resp.json().catch(()=>({}));
  alert(resp.ok ? `Queued production collectors: ${(body.queued||[]).join(', ')}` : `Could not start: ${body.error||resp.status}`);
  setTimeout(()=>location.reload(), 600);
}
</script>'''
    if is_operator:
        def _row(key, mode):
            state = (controller_snapshot.get(key) or {}).get("state", "idle")
            badge = {"idle":"badge-muted","queued":"badge-warn","running":"badge-warn","success":"badge-ok","failed":"badge-warn","blocked":"badge-warn","already_running":"badge-warn"}.get(state,"badge-muted")
            disabled = "disabled" if state in ("queued","running") else ""
            return f'<div class=collectorrow><span class=name>{e(key)}</span><span class="badge {badge}">{e(state)}</span><button class=smallbtn {disabled} onclick="runCollector(\'{e(key)}\',\'{mode}\')">Run</button></div>'
        prod_rows = "".join(_row(k, "production") for k in production_keys) or '<p class=muted>No production collectors in config/scope.yaml.</p>'
        exp_rows = "".join(_row(k, "experimental") for k in experimental_keys) or '<p class=muted>No experimental/soak collectors registered.</p>'
        collect = f'''<section class=card style="margin-top:14px" id=operations><h2>Local Collection (manual only)</h2>
<p class=muted>Nothing runs automatically. Trigger a collector explicitly below. "Run all" only runs collectors approved in config/scope.yaml — experimental/soak collectors are never included automatically.</p>
<div class=grid style="margin-top:10px"><div class=card><h3 style="margin:0 0 8px;font-size:13px">Production ({len(production_keys)})<button class=smallbtn style="float:right" onclick="runAll()">Run all</button></h3>{prod_rows}</div>
<div class=card><h3 style="margin:0 0 8px;font-size:13px">Experimental / Soak ({len(experimental_keys)}) <span class="badge badge-soak">hidden from Run all</span></h3>{exp_rows}</div></div></section>'''
    else:
        collect='' if controller is None else '''<section class=card style="margin-top:14px"><h2>Collection disabled</h2><p class=muted>This Phase 0 dashboard is read-only. No authenticated mutation profile exists; use the approved CLI workflow outside the dashboard.</p></section>'''
    attention_badges = ", ".join(f'<span class="badge badge-warn" style="margin-right:6px">{e(k)}</span>' for k in degraded_sources)
    sources_html = f'''<section class=card style="margin-top:14px" id=sources><h2>Sources</h2><div class=summary style="grid-template-columns:repeat(4,1fr)"><div class=metric><div class=label>HEALTHY</div><div class=number style="color:var(--green)">{len(healthy_sources)}</div><span class=muted>Production, last run ok</span></div><div class=metric><div class=label>DEGRADED / FAILED</div><div class=number style="color:{'var(--amber)' if degraded_sources else 'var(--muted)'}">{len(degraded_sources)}</div><span class=muted>Production, needs attention</span></div><div class=metric><div class=label>NEVER RUN</div><div class=number>{len(never_run_sources)}</div><span class=muted>Production, no runs yet</span></div><div class=metric><div class=label>EXPERIMENTAL / SOAK</div><div class=number>{len(experimental_keys)}</div><span class=muted>Registered, excluded from Run all</span></div></div>{('<p class=small style="margin-top:8px">Needs attention: ' + attention_badges + '</p>') if degraded_sources else ''}</section>'''
    page = f'''<!doctype html><html><head><meta charset=utf-8>{css}<title>Feature Phone Clank</title></head><body><div class=app><header><div class=brand>☎ Feature Phone Clank<small>HMD / Nokia classification intelligence</small></div><span class=pill>FIELD TEST MODE</span><span class=prov title="{e(str(db))}">Revision: {e(rev)} | Database: {e(db.name)}</span><span class=spacer></span><button onclick="location.reload()">↻ Refresh</button></header><aside><a class="nav active" href=#overview>⌂ Overview</a><div class=navtitle>CLASSIFICATION</div><a class=nav href=#accepted>✓ Accepted</a><a class=nav href=#ambiguous>? Ambiguous</a><a class=nav href=#rejected>⊘ Rejected Smartphones</a><a class=nav href=#incomplete>◫ Incomplete</a><div class=navtitle>CHANGES</div><a class=nav href=#events>◉ Recent Events</a><a class=nav href=#qc-history>✔ Recently QCed</a><a class=nav href=#products>⌕ Product Details</a><div class=navtitle>SYSTEM</div><a class=nav href=#sources>❖ Sources</a>{'<a class=nav href=#operations>▶ Operations</a>' if is_operator else ''}<a class=nav href=#runs>◷ Run History</a><a class=nav href=#about>ⓘ About</a></aside><main id=overview><section class=summary><div class="metric health"><div class=label>OVERALL HEALTH</div><div class=number>{'WARNING' if not runs else 'HEALTHY'}</div><span class=muted>Field-test local state</span></div><div class=metric><div class=label>ACCEPTED</div><div class=number>{len(accepted)}</div><span class=muted>Feature phones</span></div><div class=metric><div class=label>AMBIGUOUS</div><div class=number>{len(amb)}</div><span class=muted>Needs owner review</span></div><div class=metric><div class=label>REJECTED SMARTPHONES</div><div class=number>{len(rejected)}</div><span class=muted>Expected classifier outcome</span></div><div class=metric><div class=label>INCOMPLETE</div><div class=number>{len(inc)}</div><span class=muted>Present, incomplete specs</span></div></section>{collect}{sources_html}<section class=grid><div class=card><h2>Classification Overview</h2>{table(('Product','Latest observed','Source'),accepted,'No accepted feature phones','Accepted HMD/Nokia feature phones will appear here.')}</div><div class=card><h2>Local field test</h2><p class=muted>Collection uses the canonical classifier and isolated local database. External delivery is disabled.</p><a href=#runs>View Run History →</a><br><a href=#events>View Recent Events →</a><br><a href=#qc-history>View Recently QCed →</a></div></section><section class=three><div class=card id=ambiguous><h2>Ambiguous Candidates</h2>{table(('Candidate','Last observed','Stored evidence','Source'),amb,'No ambiguous candidates','No currently ambiguous products are recorded.')}</div><div class=card id=rejected><h2>Rejected Smartphones</h2>{table(('Candidate','Last observed','Stored evidence','Source'),rejected,'No rejected smartphones','Expected classifier rejections will appear here.')}</div><div class=card id=incomplete><h2>Incomplete Products</h2>{table(('Product','Completeness','Observed','Source'),inc,'No incomplete products','Incomplete specs do not mean a product disappeared.')}</div></section><section class=card id=events style="margin-top:14px"><h2>Recent Events / Changes <span class=muted style="font-weight:400">— QC'd items move to Recently QCed</span></h2>{table(('Product','Event','Timestamp','Evidence','QC decision'),ev,'No recent events','New product and change events will appear here.')}</section><section class=card id=qc-history style="margin-top:14px"><h2>Recently QCed</h2>{table(('Product','Source','Event','Decision','Decided at','Corrected?','Source link'),qcrows,'No QC decisions yet','Decisions made on Recent Events will appear here with full provenance.')}</section><section class=card id=products style="margin-top:14px"><h2>Product Detail / Identity Evidence</h2>{table(('Resolved product','Model','Presence','Completeness','Latest observation','Source'),productrows,'No products recorded','Product identity and evidence will appear here after local field-test data is present.')}</section><section class=card id=runs style="margin-top:14px"><h2>Run History</h2>{table(('Source','Started','Status','Observed'),runrows,'No runs recorded yet','Scheduled or local field-test runs will appear here.')}</section><section class=guide id=about><b>Identity safety guidance.</b> Recycled Nokia/HMD names can represent different generations: rely on model and source evidence, not marketing name alone. Incomplete specs do not equal disappearance. Rejected smartphones are expected classifier behaviour, not errors.</section></main></div>{js}</body></html>'''
    return page
def serve(host='127.0.0.1',port=8400,controller=None):
    try: loopback = ipaddress.ip_address(host).is_loopback
    except ValueError: loopback = host.lower() == 'localhost'
    if not loopback:
        raise ValueError('Feature Phone Clank has no authenticated remote profile; dashboard host must be loopback')
    from .local_collection import LocalCollectionController
    is_operator_controller = isinstance(controller, LocalCollectionController)
    # A LocalCollectionController names its own database explicitly (the
    # exact file its own collection runs write to) -- the dashboard MUST
    # read/mutate that same file, never re-derive one independently via
    # resolve_data_path()'s own env-var-dependent default, which can
    # silently diverge from what the controller was actually configured
    # with (a real bug caught by test_dashboard_operations.py: without
    # this, a controller built against an isolated/test database left the
    # dashboard's own HTTP handlers reading and writing the unrelated
    # default-path database instead).
    db = Path(controller.database).resolve() if is_operator_controller else resolve_data_path('data/feature_phone_clank.db')
    qc_db = db.with_name('feature_phone_clank_qc.db')

    class H(BaseHTTPRequestHandler):
        def _local_operator_request(self):
            """Loopback client AND loopback Host header, matching Watch
            Clank's request_is_local_operator_mutation. Defense-in-depth on
            top of serve()'s own bind-host restriction."""
            client_host = self.client_address[0] if self.client_address else None
            host_header = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return _loopback(client_host) and _loopback(host_header)

        def _json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                return {}

        def _send_json(self, code, payload):
            body = json.dumps(payload, default=str).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == '/api/local-collection/status' and controller:
                snap = controller.snapshot() if hasattr(controller, 'snapshot') else {}
                self._send_json(200, snap); return
            if path == '/healthz':
                self._send_json(200, {'status':'ok','database':str(db)}); return
            if path != '/':
                self.send_error(404)
                return
            body = render(db,controller,qc_db=qc_db).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = urlparse(self.path).path

            # Legacy Phase 0 surface: unconditionally refused, unchanged.
            if path == '/api/local-collection/run':
                if not controller:
                    self.send_error(404); return
                self.send_error(403, 'Dashboard mutations are disabled until an authenticated profile exists'); return

            # New Phase 1 local-operator surface: only for a real
            # LocalCollectionController, only from a loopback client+Host.
            if not is_operator_controller or not self._local_operator_request():
                self.send_error(404); return

            parts = path.strip('/').split('/')
            if path == '/operations/run-all':
                ok, payload = controller.start_all_production()
                self._send_json(200 if ok else 409, payload); return
            if len(parts) == 3 and parts[0] == 'operations' and parts[1] == 'run':
                ok, payload = controller.start(parts[2], mode='production')
                self._send_json(200 if ok else 409, payload); return
            if len(parts) == 3 and parts[0] == 'operations' and parts[1] == 'run-experimental':
                ok, payload = controller.start(parts[2], mode='experimental')
                self._send_json(200 if ok else 409, payload); return
            if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'qc' and parts[2] == 'review':
                try:
                    event_id = int(parts[3])
                except ValueError:
                    self.send_error(404); return
                body = self._json_body()
                decision = (body.get('decision') or '').upper()
                reason = body.get('reason')
                if decision not in DECISIONS:
                    self._send_json(400, {'error': 'invalid_decision', 'allowed': sorted(DECISIONS)}); return
                store = SqliteStore(str(db))
                try:
                    row = store.db.execute(
                        "SELECT e.*, p.product_key, p.manufacturer, p.model, p.model_number, p.url "
                        "FROM events e JOIN products p ON p.id = e.product_id WHERE e.id=?", (event_id,)
                    ).fetchone()
                    if row is None:
                        self._send_json(404, {'error': 'event_not_found'}); return
                    run_row = store.db.execute(
                        "SELECT id, started_at FROM collector_runs WHERE source_key=? AND started_at<=? "
                        "ORDER BY started_at DESC LIMIT 1", (row['collector'], row['detected_at']),
                    ).fetchone()
                finally:
                    store.close()
                qc_store = QcArchiveStore(str(qc_db))
                try:
                    result = qc_store.submit_review(
                        event_id=row['id'], source_key=row['collector'], event_type=row['event_type'],
                        decision=decision, product_key=row['product_key'], manufacturer=row['manufacturer'],
                        model=row['model'], model_number=row['model_number'], url=row['url'],
                        changed_fields=json.loads(row['changed_fields_json'] or '[]'),
                        meta=json.loads(row['meta_json'] or '{}'), detected_at=row['detected_at'],
                        run_id=run_row['id'] if run_row else None,
                        run_started_at=run_row['started_at'] if run_row else None,
                        reason=reason,
                    )
                finally:
                    qc_store.close()
                self._send_json(200, {'status': 'ok', 'review': result}); return

            self.send_error(404)

        def log_message(self,*_): pass
    return ThreadingHTTPServer((host,port),H)
