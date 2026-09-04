"""Clank Collector UI - shared design system v1 (generated; do not hand-edit).

One internal product suite, six domain personalities. This module is copied
verbatim into every collector Clank and pinned byte-for-byte by a test in
each repo, so the six dashboards share one visual language WITHOUT any of
them gaining a runtime dependency on a shared service or network asset.

Shipping the stylesheet as a Python constant (rather than a static file)
keeps it working identically under PyInstaller: a module is bundled by
import-graph analysis automatically, a stray .css file is not.

Each Clank overrides the accent tokens only, via shell(). Status is never
carried by colour alone - badge() always renders a text label.
"""

from __future__ import annotations

from html import escape as _e

DESIGN_SYSTEM_VERSION = "collector-ui-v1"

CSS = r"""/* ==========================================================================
   CLANK COLLECTOR UI - shared design system v1
   --------------------------------------------------------------------------
   One internal product suite, six domain personalities. This file is the
   single source of truth for the collector family visual language and is
   copied verbatim into each collector repo (a test pins it byte-for-byte),
   so no dashboard gains a runtime dependency on a shared service.

   Each Clank overrides --accent and --accent-soft ONLY. Everything else is
   shared. Status is never carried by colour alone: every state has a text
   label, and the badge dot is decoration on top of that label.
   ========================================================================== */

:root {
  /* Surfaces */
  --bg:            #0d1117;
  --surface:       #151b23;
  --surface-2:     #1b222c;
  --surface-3:     #222b37;
  --line:          #2a3441;
  --line-strong:   #3a4757;

  /* Text */
  --text:          #e6edf3;
  --text-dim:      #adbac7;
  --muted:         #7d8b9a;

  /* Domain accent - overridden per Clank */
  --accent:        #4c8dff;
  --accent-soft:   #16283f;

  /* Semantic status */
  --ok:            #3fb950;   --ok-soft:      #12261a;
  --warn:          #d29922;   --warn-soft:    #2b2213;
  --bad:           #f85149;   --bad-soft:     #2d1618;
  --info:          #58a6ff;   --info-soft:    #12233b;
  --idle:          #8b949e;   --idle-soft:    #1e242c;

  /* Spacing scale */
  --s1: 4px;  --s2: 8px;  --s3: 12px; --s4: 16px;
  --s5: 24px; --s6: 32px; --s7: 48px;

  /* Shape */
  --r1: 4px;  --r2: 6px;  --r3: 10px;

  /* Typography */
  --font: ui-sans-serif, -apple-system, Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif;
  --mono: ui-monospace, Cascadia Mono, SFMono-Regular, Menlo, Consolas, monospace;
  --fs-page:  20px;   --fs-section: 14px;  --fs-body: 13px;
  --fs-meta:  12px;   --fs-label:   11px;  --fs-kpi:  27px;

  /* Layout - wide-monitor first (1080p/1440p operator desktops) */
  --maxw: 1680px;
  --rail: 210px;
}

* { box-sizing: border-box; }

html, body {
  margin: 0; padding: 0;
  background: var(--bg); color: var(--text);
  font-family: var(--font); font-size: var(--fs-body);
  line-height: 1.45;
  -webkit-font-smoothing: antialiased;
}

a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ------------------------------------------------------------------ shell */

.app { min-height: 100vh; display: flex; flex-direction: column; }

.topbar {
  display: flex; align-items: center; gap: var(--s4);
  padding: 0 var(--s5); height: 52px;
  background: var(--surface); border-bottom: 1px solid var(--line);
  position: sticky; top: 0; z-index: 40;
}
.brand { display: flex; align-items: center; gap: var(--s2); min-width: 0; }
.brand-mark {
  width: 22px; height: 22px; border-radius: var(--r2);
  background: var(--accent); color: #06101d;
  display: inline-flex; align-items: center; justify-content: center;
  font-weight: 800; font-size: 12px; flex: none;
}
.brand-name {
  font-size: 15px; font-weight: 700; letter-spacing: -0.01em;
  color: var(--text); white-space: nowrap;
}
.brand-suite {
  font-size: var(--fs-label); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.09em; white-space: nowrap;
}
.topbar-meta {
  margin-left: auto; display: flex; align-items: center; gap: var(--s3);
  font-size: var(--fs-meta); color: var(--muted); white-space: nowrap;
}

.body { display: flex; flex: 1; min-height: 0; }

.rail {
  width: var(--rail); flex: none;
  background: var(--surface); border-right: 1px solid var(--line);
  padding: var(--s4) var(--s3); display: flex; flex-direction: column; gap: 2px;
}
.rail-group {
  font-size: var(--fs-label); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.09em;
  margin: var(--s4) var(--s2) var(--s1);
}
.rail-group.first { margin-top: 0; }
.nav {
  display: flex; align-items: center; gap: var(--s2);
  padding: 7px var(--s3); border-radius: var(--r2);
  color: var(--text-dim); font-size: var(--fs-body); font-weight: 500;
}
.nav:hover { background: var(--surface-2); color: var(--text); text-decoration: none; }
.nav.active { background: var(--accent-soft); color: var(--accent); font-weight: 650; }
.nav .count { margin-left: auto; font-size: var(--fs-label); color: var(--muted); }

.main { flex: 1; min-width: 0; padding: var(--s5); }
.wrap { max-width: var(--maxw); margin: 0 auto; }

.page-head { margin-bottom: var(--s5); }
.page-title { font-size: var(--fs-page); font-weight: 700; margin: 0; letter-spacing: -0.02em; }
.page-sub { color: var(--muted); font-size: var(--fs-meta); margin: var(--s1) 0 0; }

.foot {
  border-top: 1px solid var(--line); padding: var(--s3) var(--s5);
  color: var(--muted); font-size: var(--fs-meta);
  display: flex; gap: var(--s4); flex-wrap: wrap;
}

/* ----------------------------------------------------------------- panels */

.panel {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--r3); margin-bottom: var(--s4);
}
.panel-head {
  display: flex; align-items: center; gap: var(--s3);
  padding: var(--s3) var(--s4); border-bottom: 1px solid var(--line);
}
.panel-title { font-size: var(--fs-section); font-weight: 650; margin: 0; }
.panel-sub { font-size: var(--fs-meta); color: var(--muted); margin-left: auto; }
.panel-body { padding: var(--s4); }
.panel-body.flush { padding: 0; }
/* Self-padding panel for simple content that needs no header row. */
.panel.pad { padding: var(--s4); }

/* --------------------------------------------------------------- kpi grid */

.kpis {
  display: grid; gap: var(--s3); margin-bottom: var(--s4);
  grid-template-columns: repeat(auto-fit, minmax(178px, 1fr));
}
.kpi {
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--r3); padding: var(--s3) var(--s4);
}
.kpi-label {
  font-size: var(--fs-label); color: var(--muted);
  text-transform: uppercase; letter-spacing: 0.07em; font-weight: 600;
}
.kpi-value {
  font-size: var(--fs-kpi); font-weight: 700; line-height: 1.15;
  margin-top: 2px; letter-spacing: -0.02em;
}
.kpi-value.sm { font-size: 17px; }
.kpi-note { font-size: var(--fs-meta); color: var(--muted); margin-top: 2px; }
.kpi.is-ok   { border-color: #1e4429; }
.kpi.is-warn { border-color: #4a3a15; }
.kpi.is-bad  { border-color: #4d2225; }

/* --------------------------------------------------------------- statuses */
/* Text always carries the meaning; colour is supplementary. */

.badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px var(--s2); border-radius: 999px;
  font-size: var(--fs-label); font-weight: 650; letter-spacing: 0.03em;
  background: var(--idle-soft); color: var(--idle);
  border: 1px solid transparent; white-space: nowrap;
}
.badge::before { content: "\25CF"; font-size: 8px; line-height: 1; }
.badge.ok       { background: var(--ok-soft);   color: var(--ok);   border-color: #1e4429; }
.badge.warn     { background: var(--warn-soft); color: var(--warn); border-color: #4a3a15; }
.badge.bad      { background: var(--bad-soft);  color: var(--bad);  border-color: #4d2225; }
.badge.info     { background: var(--info-soft); color: var(--info); border-color: #1d3a5c; }
.badge.accent   { background: var(--accent-soft); color: var(--accent); }
.badge.plain::before { content: none; }
.badge.sq { border-radius: var(--r1); }

/* ---------------------------------------------------------------- tables */

.tablewrap { overflow-x: auto; }
/* Long tables opt into their own scroll box; only then do sticky headers make
   sense, and they stick to that box rather than to the page. */
.tablewrap.scroll { max-height: 62vh; overflow-y: auto; }
.tablewrap.scroll table.t thead th { position: sticky; top: 0; z-index: 5; }
table.t { width: 100%; border-collapse: collapse; font-size: var(--fs-body); }
table.t thead th {
  /* NOT sticky by default: a panel is page-flow, not a scroll container, so a
     sticky header offset to the topbar floats over the first row. Opt in with
     .tablewrap.scroll, which supplies the scroll context the sticky needs. */
  background: var(--surface-2); color: var(--muted);
  font-size: var(--fs-label); font-weight: 650;
  text-transform: uppercase; letter-spacing: 0.06em;
  text-align: left; padding: var(--s2) var(--s3);
  border-bottom: 1px solid var(--line);
}
table.t tbody td {
  padding: 9px var(--s3); border-bottom: 1px solid var(--line);
  vertical-align: middle;
}
table.t tbody tr:last-child td { border-bottom: none; }
table.t tbody tr:hover { background: var(--surface-2); }
table.t td.num, table.t th.num { text-align: right; font-variant-numeric: tabular-nums; }
table.t td.mono, .mono { font-family: var(--mono); font-size: var(--fs-meta); }
table.t td.dim { color: var(--muted); }
table.t td.wrap-any { white-space: normal; word-break: break-word; }

/* ----------------------------------------------------------- empty/error */

.empty { padding: var(--s6) var(--s4); text-align: center; color: var(--muted); }
.empty-title { color: var(--text-dim); font-weight: 600; margin-bottom: var(--s1); }
.empty-hint { font-size: var(--fs-meta); }

.notice {
  display: flex; gap: var(--s3); align-items: flex-start;
  padding: var(--s3) var(--s4); border-radius: var(--r3);
  border: 1px solid var(--line); background: var(--surface-2);
  margin-bottom: var(--s4); font-size: var(--fs-body);
}
.notice .notice-body { min-width: 0; }
.notice .notice-title { font-weight: 650; }
.notice .notice-text { color: var(--text-dim); font-size: var(--fs-meta); margin-top: 2px; }
.notice.warn { border-color: #4a3a15; background: var(--warn-soft); }
.notice.bad  { border-color: #4d2225; background: var(--bad-soft); }
.notice.info { border-color: #1d3a5c; background: var(--info-soft); }

details.raw { margin-top: var(--s3); }
details.raw summary { cursor: pointer; color: var(--muted); font-size: var(--fs-meta); padding: var(--s1) 0; }
details.raw pre {
  background: var(--bg); border: 1px solid var(--line); border-radius: var(--r2);
  padding: var(--s3); overflow-x: auto; font-family: var(--mono);
  font-size: var(--fs-meta); color: var(--text-dim); margin: var(--s2) 0 0;
}

/* --------------------------------------------------------------- controls */

.btn {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 6px var(--s3); border-radius: var(--r2);
  font-size: var(--fs-body); font-weight: 600; font-family: inherit;
  border: 1px solid var(--line-strong); background: var(--surface-2);
  color: var(--text); cursor: pointer; line-height: 1.4;
}
.btn:hover:not(:disabled) { background: var(--surface-3); text-decoration: none; }
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #06101d; }
.btn.primary:hover:not(:disabled) { filter: brightness(1.08); }
.btn.tertiary { background: transparent; border-color: transparent; color: var(--text-dim); }
.btn.tertiary:hover:not(:disabled) { background: var(--surface-2); color: var(--text); }
.btn.danger { border-color: #5b2a2c; color: var(--bad); background: var(--bad-soft); }
.btn.sm { padding: 3px var(--s2); font-size: var(--fs-label); }

.filters {
  display: flex; gap: var(--s2); align-items: center; flex-wrap: wrap;
  padding: var(--s3) var(--s4); border-bottom: 1px solid var(--line);
}
input.f, select.f {
  background: var(--bg); color: var(--text); font-family: inherit;
  border: 1px solid var(--line-strong); border-radius: var(--r2);
  padding: 5px var(--s2); font-size: var(--fs-body);
}
input.f:focus, select.f:focus { outline: 2px solid var(--accent-soft); border-color: var(--accent); }

/* ------------------------------------------------------------ definitions */

.dl { display: grid; grid-template-columns: max-content 1fr; gap: var(--s2) var(--s4); }
.dl dt { color: var(--muted); font-size: var(--fs-meta); }
.dl dd { margin: 0; font-size: var(--fs-body); }

.cols { display: grid; gap: var(--s4); grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
.cols.two { grid-template-columns: repeat(auto-fit, minmax(430px, 1fr)); }

/* Secondary label under a primary one (source id under a source name, an
   action description under an action). Several Clanks emit <small> for
   this inline, which ran the two strings together. */
small { display: block; color: var(--muted); font-size: var(--fs-label);
        font-weight: 400; letter-spacing: 0; }
.kpi p, .kpi .kpi-note { font-size: var(--fs-meta); margin: var(--s1) 0 0; }

.stack { display: flex; flex-direction: column; gap: var(--s1); }
.row   { display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap; }
.muted { color: var(--muted); }
.dim   { color: var(--text-dim); }
.right { margin-left: auto; }
.nowrap { white-space: nowrap; }

/* ------------------------------------------------------------- responsive */

@media (max-width: 1000px) {
  .body { flex-direction: column; }
  .rail {
    width: auto; flex-direction: row; overflow-x: auto;
    border-right: none; border-bottom: 1px solid var(--line);
    padding: var(--s2) var(--s3);
  }
  .rail-group { display: none; }
  .nav { white-space: nowrap; }
  .main { padding: var(--s4); }
}
"""


# Canonical status vocabulary shared across the collector family. The label is
# the meaning; the tone only tints it.
TONES = {
    "HEALTHY": "ok", "DEGRADED": "warn", "FAILED": "bad", "BLOCKED": "bad",
    "UNKNOWN": "", "DISABLED": "",
    "PRODUCTION": "ok", "EXPERIMENTAL": "warn",
    "SCHEDULED": "info", "MANUAL": "",
    "RUNNING": "info", "SUCCESS": "ok", "PARTIAL": "warn",
    "DELIVERED": "ok", "QUEUED": "info", "DELIVERY FAILED": "bad",
    "SUPPRESSED": "warn", "NOT ATTEMPTED": "",
}


def badge(label, tone=None, plain=False):
    """A status chip whose TEXT carries the meaning (never colour alone)."""
    if label is None or label == "":
        label = "UNKNOWN"
    text = str(label).replace("_", " ").upper()
    if tone is None:
        tone = TONES.get(text, "")
    cls = "badge" + ((" " + tone) if tone else "") + (" plain" if plain else "")
    return "<span class=" + chr(34) + cls + chr(34) + ">" + _e(text) + "</span>"


def empty(title, hint=""):
    """An empty state that says what the absence MEANS. Never renders 'Empty'."""
    out = "<div class=" + chr(34) + "empty" + chr(34) + "><div class=" + chr(34) + "empty-title" + chr(34) + ">" + _e(title) + "</div>"
    if hint:
        out += "<div class=" + chr(34) + "empty-hint" + chr(34) + ">" + _e(hint) + "</div>"
    return out + "</div>"


def kpi(label, value, note="", tone=""):
    q = chr(34)
    cls = "kpi" + ((" is-" + tone) if tone else "")
    small = " sm" if isinstance(value, str) and len(str(value)) > 8 else ""
    out = "<div class=" + q + cls + q + "><div class=" + q + "kpi-label" + q + ">" + _e(label) + "</div>"
    out += "<div class=" + q + "kpi-value" + small + q + ">" + str(value) + "</div>"
    if note:
        out += "<div class=" + q + "kpi-note" + q + ">" + _e(note) + "</div>"
    return out + "</div>"


def nav(items, active):
    """items: iterable of (href, label, group); group may repeat or be None."""
    q = chr(34)
    out, seen, first = [], None, True
    for href, label, group in items:
        if group != seen:
            seen = group
            if group:
                cls = "rail-group first" if first else "rail-group"
                out.append("<div class=" + q + cls + q + ">" + _e(group) + "</div>")
                first = False
        cls = "nav active" if active == href else "nav"
        out.append("<a class=" + q + cls + q + " href=" + q + _e(href) + q + ">" + _e(label) + "</a>")
    return "".join(out)


def shell(clank, initials, accent, accent_soft, nav_html, title, subtitle,
          content, meta="", footer="", head_extra=""):
    """The common application shell every collector Clank renders inside."""
    q = chr(34)
    sub = ("<p class=" + q + "page-sub" + q + ">" + _e(subtitle) + "</p>") if subtitle else ""
    return "".join([
        "<!DOCTYPE html><html lang=", q, "en", q, "><head><meta charset=", q, "utf-8", q, ">",
        "<meta name=", q, "viewport", q, " content=", q, "width=device-width,initial-scale=1", q, ">",
        "<title>", _e(title), " · ", _e(clank), "</title>",
        "<style>", CSS, chr(10), ":root{--accent:", accent, ";--accent-soft:", accent_soft, ";}</style>",
        head_extra,
        "</head><body><div class=", q, "app", q, ">",
        "<header class=", q, "topbar", q, "><div class=", q, "brand", q, ">",
        "<span class=", q, "brand-mark", q, ">", _e(initials), "</span>",
        "<span class=", q, "brand-name", q, ">", _e(clank), "</span>",
        "<span class=", q, "brand-suite", q, ">Clank Fleet</span></div>",
        "<div class=", q, "topbar-meta", q, ">", meta, "</div></header>",
        "<div class=", q, "body", q, "><nav class=", q, "rail", q, ">", nav_html, "</nav>",
        "<main class=", q, "main", q, "><div class=", q, "wrap", q, ">",
        "<div class=", q, "page-head", q, "><h1 class=", q, "page-title", q, ">", _e(title), "</h1>",
        sub, "</div>", content,
        "</div></main></div>",
        "<footer class=", q, "foot", q, ">", footer, "</footer>",
        "</div></body></html>",
    ])
