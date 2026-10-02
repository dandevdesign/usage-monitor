from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

def _default_hermes_root() -> str:
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        pass
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if base:
            return os.path.join(base, "hermes")
        return os.path.join(os.path.expanduser("~"), "AppData", "Local", "hermes")
    return os.path.expanduser("~/.hermes")


def _default_omni_db(hermes_root: str) -> str:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        cand = os.path.join(base, "omniroute", "storage.sqlite") if base else ""
        if cand and os.path.exists(cand):
            return cand
        cand2 = os.path.join(os.path.expanduser("~"), ".omniroute", "storage.sqlite")
        if os.path.exists(cand2):
            return cand2
        return cand or cand2
    return os.path.expanduser("~/.omniroute/storage.sqlite")


HERMES_ROOT = os.environ.get("HERMES_HOME", "").strip() or _default_hermes_root()
OMNI_DB = os.environ.get("OMNIROUTE_DB", "").strip() or _default_omni_db(HERMES_ROOT)
CACHE_TTL = 5.0          # seconds; keeps rapid refreshes from hammering SQLite
_cache: dict = {}        # hours -> (timestamp, payload); per-window, not global
_lock = threading.Lock()


# ── db helpers ──────────────────────────────────────────────────────────────
def ro(path: str):
    if not os.path.exists(path):
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def has_table(c, name: str) -> bool:
    try:
        return bool(c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())
    except sqlite3.Error:
        return False


def profile_dbs() -> list[tuple[str, str]]:
    out = []
    root = os.path.join(HERMES_ROOT, "state.db")
    if os.path.exists(root):
        out.append(("default", root))
    pdir = os.path.join(HERMES_ROOT, "profiles")
    if os.path.isdir(pdir):
        for n in sorted(os.listdir(pdir)):
            db = os.path.join(pdir, n, "state.db")
            if os.path.exists(db):
                out.append((n, db))
    return out


def since_iso(hours: float) -> str:
    """Cutoff for OmniRoute call_logs, whose timestamps are UTC ISO ('…T…Z').

    Must be UTC and identical in shape to the stored value, otherwise the
    lexicographic comparison silently drifts: a local-time, space-separated
    cutoff made every window shorter than 24h return the whole day.
    """
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


def _ago(ts) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromtimestamp(float(ts))
    except (TypeError, ValueError):
        return ""
    d = (datetime.now() - dt).total_seconds()
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def _age_seconds(ts) -> float | None:
    """Seconds since a unix timestamp, or None when unknown."""
    if not ts:
        return None
    try:
        return max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return None


def _parse_iso(value) -> float | None:
    """ISO-8601 (possibly with offset/Z) -> unix timestamp."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ── provider naming ─────────────────────────────────────────────────────────
# A session can move between providers mid-life, so billing_provider alone is
# not enough: "custom" means "the default custom endpoint", and the port in
# billing_base_url is what actually tells you WHO answered.
_PROVIDER_HINTS = {
    "8090": "openzen",
    "20128": "omniroute",
    "20130": "omniroute-alt",
}


def _base_host(url: str | None) -> str:
    if not url:
        return ""
    try:
        p = urlparse(str(url))
    except ValueError:
        return str(url)[:60]
    host = p.hostname or ""
    if not host:
        return str(url)[:60]
    return f"{host}:{p.port}" if p.port else host


def _provider_name(billing_provider, billing_base_url) -> str:
    prov = str(billing_provider or "").strip()
    if prov.startswith("custom:"):
        return prov.split(":", 1)[1]
    if prov and prov != "custom":
        return prov
    host = _base_host(billing_base_url)
    port = host.rsplit(":", 1)[-1] if ":" in host else ""
    return _PROVIDER_HINTS.get(port) or host or "custom"



# ── failure forensics: WHY a model fell ─────────────────────────────────────
# ~/.hermes/logs/agent.log and errors.log carry the real reason, with the
# session id, the error class and the upstream's own words:
#   2026-10-01 10:00:09,389 WARNING [20261001_095441_0a686e3e] agent.conversation_loop:
#     API call failed (attempt 1/3) error_type=APIConnectionError thread=bg-review:…
#     provider=custom base_url=http://127.0.0.1:8090/v1 model=mimo-v2.6-flash-free
#     summary=Connection error.
# errors.log mirrors agent.log, so events are de-duplicated by content.
LOG_DIR = os.path.join(HERMES_ROOT, "logs")
GATEWAY_STATE_FILE = os.path.join(HERMES_ROOT, "gateway_state.json")
LOG_TAIL_BYTES = 4 * 1024 * 1024        # per file; logs rotate at ~5 MB
MAX_LOG_EVENTS = 3000
SESSION_ROUTE_LIMIT = 300

_log_cache: dict = {}                    # path -> (size, mtime, events)
_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
_SESSION_RE = re.compile(r"\[([^\]\s]+)\]")
_ATTEMPT_RE = re.compile(r"\((attempt[^)]*)\)")
_FIELD_RES = {
    "etype": re.compile(r"error_type=(\S+)"),
    "provider": re.compile(r"provider=(\S+)"),
    "base_url": re.compile(r"base_url=(\S+)"),
    "model": re.compile(r"model=(\S+)"),
    "summary": re.compile(r"summary=(.*)$"),
}


def _why(etype: str, summary: str = "", status=None) -> str:
    """Plain-language reason for a failure, from error class + status + text."""
    e = (etype or "").lower()
    s = (summary or "").lower()
    if "fingerprint" in e or "fingerprint" in s:
        return "Cloudflare fingerprint rejection (403) — provider refuses this client"
    if status == 499 or "request aborted" in s:
        return "client aborted the call — NOT a model failure"
    if "rate" in e or status == 429 or "rate limit" in s or "quota" in s or "too many" in s:
        return "rate limit / quota exhausted — provider throttled us"
    if "auth" in e or status in (401, 403) or "invalid api key" in s or "unauthorized" in s:
        return "bad or expired credentials for this provider"
    if ("server_error" in e or "bad_gateway" in e or "connect" in e or status in (502, 503, 504)
            or "deadline" in s or "timeout" in s or "connection error" in s):
        return "upstream unreachable / provider error (network, down, or timeout)"
    if "not_found" in e or status == 404 or "not found" in s:
        return "model not offered at that endpoint — routing hits the wrong base_url"
    if status == 400 or "validation" in s or "protocol" in s or "not support" in s:
        return "endpoint rejected the request format (wrong API for this model)"
    if "stream" in s and "failed" in s:
        return "upstream stream broke mid-response"
    if e.startswith("api") or e.endswith("error"):
        return "upstream API error — see raw summary"
    return "unclassified — see raw summary"


def _parse_log_file(path: str) -> list:
    """Tail-parse one log file for failure events (cached by size+mtime)."""
    try:
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _log_cache.get(path)
    if hit and hit[0] == size and hit[1] == mtime:
        return hit[2]
    try:
        with open(path, "rb") as fh:
            fh.seek(max(0, size - LOG_TAIL_BYTES))
            raw = fh.read().decode("utf-8", "replace")
    except OSError:
        return []
    events = []
    for line in raw.splitlines():
        if "API call failed" not in line and "Model fallback" not in line:
            continue
        m = _TS_RE.match(line)
        if not m:
            continue
        ev = {"ts": m.group(1), "session": "", "etype": "", "provider": "",
              "base_url": "", "model": "", "attempt": "", "summary": "",
              "kind": "fallback" if "fallback" in line else "api_error"}
        sm = _SESSION_RE.search(line[:80])   # the session tag sits right after the level
        if sm:
            ev["session"] = sm.group(1)
        am = _ATTEMPT_RE.search(line)
        if am:
            ev["attempt"] = am.group(1)
        for key, rx in _FIELD_RES.items():
            fm = rx.search(line)
            if fm:
                ev[key] = fm.group(1).strip()
        events.append(ev)
    events = events[-MAX_LOG_EVENTS:]
    _log_cache[path] = (size, mtime, events)
    return events


def parse_agent_logs(hours: float) -> dict:
    """Failure events from the Hermes agent logs inside the window, aggregated."""
    cutoff = datetime.now() - timedelta(hours=hours)
    seen, events = set(), []
    if os.path.isdir(LOG_DIR):
        for name in sorted(os.listdir(LOG_DIR)):
            if not (name.startswith("agent.log") or name.startswith("errors.log")):
                continue
            for ev in _parse_log_file(os.path.join(LOG_DIR, name)):
                try:
                    dt = datetime.strptime(ev["ts"], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if dt < cutoff:
                    continue
                key = (ev["ts"], ev["session"], ev["model"], ev["etype"], ev["summary"])
                if key in seen:              # errors.log mirrors agent.log
                    continue
                seen.add(key)
                ev = dict(ev)
                ev["ago"] = _ago(dt.timestamp())
                ev["why"] = _why(ev["etype"], ev["summary"])
                events.append(ev)
    events.sort(key=lambda e: e["ts"], reverse=True)

    buckets: dict = {"type": {}, "model": {}, "session": {}}
    for ev in events:
        for kind, key in (("type", ev["etype"] or "unknown"),
                          ("model", f"{ev['model'] or '?'} @ {ev['provider'] or '?'}"),
                          ("session", ev["session"] or "-")):
            row = buckets[kind].setdefault(key, {"n": 0, "last": ev["ts"], "whys": {}})
            row["n"] += 1
            row["last"] = max(row["last"], ev["ts"])
            row["whys"][ev["why"]] = row["whys"].get(ev["why"], 0) + 1

    def _rows(kind: str) -> list:
        out = []
        for key, row in buckets[kind].items():
            top = max(row["whys"].items(), key=lambda kv: kv[1])[0]
            try:
                ago = _ago(datetime.strptime(row["last"], "%Y-%m-%d %H:%M:%S").timestamp())
            except ValueError:
                ago = ""
            out.append({"key": key, "n": row["n"], "last": row["last"][11:16],
                        "last_ago": ago, "why": top})
        out.sort(key=lambda r: (-r["n"], r["key"]))
        return out

    return {
        "total": len(events),
        "fallbacks": sum(1 for e in events if e["kind"] == "fallback"),
        "events": events[:150],
        "by_type": _rows("type"),
        "by_model": _rows("model"),
        "by_session": _rows("session"),
    }


def read_gateway_state() -> dict:
    """Who is serving right now: gateway liveness + platform connections."""
    try:
        with open(GATEWAY_STATE_FILE, "r", encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(st, dict):
        return {}
    plats = []
    for name, p in (st.get("platforms") or {}).items():
        if not isinstance(p, dict):
            continue
        plats.append({
            "name": name,
            "state": p.get("state") or "?",
            "attention": bool(p.get("needs_attention")),
            "error": (p.get("error_message") or "")[:160],
        })
    plats.sort(key=lambda p: (p["state"] != "connected", p["name"]))
    return {
        "state": st.get("gateway_state") or "?",
        "pid": st.get("pid"),
        "version": st.get("code_version"),
        "active_agents": st.get("active_agents"),
        "updated": _ago(_parse_iso(st.get("updated_at"))),
        "exit_reason": st.get("exit_reason") or "",
        "restart_requested": bool(st.get("restart_requested")),
        "session_store": (st.get("session_store") or {}).get("status") or "?",
        "profiles": st.get("served_profiles") or [],
        "platforms": plats[:12],
    }


def build(hours: float) -> dict:
    """Everything the page needs, in one pass."""
    since = since_iso(hours)
    data: dict = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_hours": hours,
        "profiles": [], "models_24h": [], "by_profile_model": [],
        "live_sessions": [], "failures": [], "combos": [], "totals": {},
        "session_routes": [], "session_switches": [], "provider_failures": [],
        "recent_provider_errors": [], "agent_failures": {}, "gateway": {},
    }
    # Forensic sources (read-only): Hermes agent logs explain WHY a call failed,
    # OmniRoute call_logs explain WHAT the upstream answered.
    logs = parse_agent_logs(hours)
    fails_by_session = {r["key"]: r for r in logs["by_session"]}
    data["agent_failures"] = logs
    data["gateway"] = read_gateway_state()

    for prof, db in profile_dbs():
        c = ro(db)
        if not c or not has_table(c, "session_model_usage"):
            continue
        rows = c.execute("""
            SELECT model, billing_provider, count(DISTINCT session_id) sessions,
                   sum(api_call_count) calls, sum(input_tokens) tin,
                   sum(output_tokens) tout, sum(cache_read_tokens) cread
            FROM session_model_usage
            GROUP BY model, billing_provider
            ORDER BY sum(input_tokens) DESC
        """).fetchall()
        prof_total = sum(r["tin"] or 0 for r in rows)
        data["profiles"].append({
            "name": prof, "total_in": prof_total,
            "total_calls": sum(r["calls"] or 0 for r in rows),
            "models": [{
                "model": r["model"], "provider": r["billing_provider"],
                "sessions": r["sessions"], "calls": r["calls"] or 0,
                "tin": r["tin"] or 0, "tout": r["tout"] or 0, "cread": r["cread"] or 0,
                "pct": round(100.0 * (r["tin"] or 0) / prof_total, 1) if prof_total else 0,
            } for r in rows[:40]],
        })
        for m in rows[:40]:
            data["by_profile_model"].append({
                "profile": prof, "model": m["model"], "provider": m["billing_provider"],
                "calls": m["calls"] or 0, "tin": m["tin"] or 0, "tout": m["tout"] or 0,
            })

        # Per-session route detail FIRST: which model, which PROVIDER served it, on
        # which endpoint. A session can move between providers mid-life (never-fail
        # via omniroute after a fallback away from antigravity), so the
        # sessions.model column alone does NOT tell you who actually answered.
        routes_by_session: dict = {}
        cutoff_ts = time.time() - hours * 3600
        if has_table(c, "session_model_usage"):
            detail = c.execute("""
                SELECT session_id, model, billing_provider, billing_base_url, billing_mode,
                       sum(api_call_count) calls, sum(input_tokens) tin,
                       sum(output_tokens) tout, min(first_seen) fs, max(last_seen) ls
                FROM session_model_usage
                GROUP BY session_id, model, billing_provider, billing_base_url, billing_mode
                ORDER BY max(last_seen) DESC LIMIT ?
            """, (SESSION_ROUTE_LIMIT,)).fetchall()
            for r in detail:
                if (r["ls"] or 0.0) < cutoff_ts:
                    continue                     # nothing from this session in the window
                data["session_routes"].append({
                    "profile": prof, "session_id": r["session_id"], "model": r["model"],
                    "provider": _provider_name(r["billing_provider"], r["billing_base_url"]),
                    "raw_provider": r["billing_provider"] or "",
                    "endpoint": _base_host(r["billing_base_url"]),
                    "mode": r["billing_mode"] or "",
                    "calls": r["calls"] or 0, "tin": r["tin"] or 0, "tout": r["tout"] or 0,
                    "first": _ago(r["fs"]), "last": _ago(r["ls"]),
                })
                routes_by_session.setdefault((prof, r["session_id"]), []).append({
                    "model": r["model"],
                    "provider": _provider_name(r["billing_provider"], r["billing_base_url"]),
                    "endpoint": _base_host(r["billing_base_url"]),
                    "mode": r["billing_mode"] or "",
                    "calls": r["calls"] or 0, "first": _ago(r["fs"]), "last": _ago(r["ls"]),
                    "_fs": r["fs"] or 0.0, "_ls": r["ls"] or 0.0,
                })

            # A mid-session model/provider change: the chain, and whether the
            # session also logged failures (the usual reason for a switch).
            # Only sessions still warm in the window — old ones are noise here.
            for (s_prof, s_id), routes in routes_by_session.items():
                if len(routes) < 2:
                    continue
                routes.sort(key=lambda x: x["_fs"])
                recent_ls = max(r["_ls"] for r in routes)
                if recent_ls < cutoff_ts:
                    continue
                fail = fails_by_session.get(s_id) or {}
                data["session_switches"].append({
                    "profile": s_prof, "session_id": s_id, "hops": len(routes),
                    "chain": " -> ".join(f"{r['model']} @ {r['provider']}" for r in routes),
                    "first": routes[0]["first"], "last": routes[-1]["last"],
                    "calls": sum(r["calls"] for r in routes),
                    "fails": fail.get("n", 0), "why": fail.get("why", ""),
                    "endpoint": ", ".join(sorted({r["endpoint"] for r in routes if r["endpoint"]})),
                    "mode": ", ".join(sorted({r["mode"] for r in routes if r["mode"]})),
                    "_ls": recent_ls,
                })

        if has_table(c, "sessions"):
            live = c.execute("""
                SELECT id, model, title, started_at, last_activity_at,
                       api_call_count, input_tokens, output_tokens
                FROM sessions WHERE ended_at IS NULL
                ORDER BY coalesce(last_activity_at, started_at) DESC LIMIT 25
            """).fetchall()
            for r in live:
                # "Open" is not the same as "active": plenty of sessions are never
                # formally closed, so recency decides.
                age = _age_seconds(r["last_activity_at"])
                if age is None:
                    state = "unknown"
                elif age < 900:
                    state = "active"
                elif age < 21600:
                    state = "idle"
                else:
                    state = "stale"
                routes = sorted(routes_by_session.get((prof, r["id"]), []),
                                key=lambda x: x["_ls"])
                last = routes[-1] if routes else None
                fail = fails_by_session.get(r["id"]) or {}
                data["live_sessions"].append({
                    "profile": prof, "id": r["id"], "model": r["model"],
                    "title": (r["title"] or "")[:70],
                    "started": _ago(r["started_at"]), "last_seen": _ago(r["last_activity_at"]),
                    "age_s": int(age) if age is not None else None, "state": state,
                    "last_route": f"{last['model']} @ {last['provider']}" if last else "",
                    "last_route_endpoint": last["endpoint"] if last else "",
                    "hops": len(routes),
                    "fails": fail.get("n", 0), "fail_why": fail.get("why", ""),
                    "calls": r["api_call_count"] or 0,
                    "tin": r["input_tokens"] or 0, "tout": r["output_tokens"] or 0,
                })
            data["live_sessions"].sort(key=lambda s: (
                s["state"] not in ("active", "idle"),
                s["age_s"] if s["age_s"] is not None else 9e9))

    # Ranked after every profile has contributed (sorting inside the loop would
    # break as soon as a second profile appended its own rows).
    data["session_switches"].sort(key=lambda s: -s.get("_ls", 0.0))
    del data["session_switches"][25:]
    for _s in data["session_switches"]:
        _s.pop("_ls", None)
    del data["session_routes"][400:]        # newest-first per profile, keep it lean

    o = ro(OMNI_DB)
    if o and has_table(o, "call_logs"):
        rows = o.execute("""
            SELECT model, provider, count(*) n, sum(tokens_in) tin,
                   sum(tokens_out) tout, round(avg(tokens_in)) avg_ctx
            FROM call_logs
            WHERE timestamp >= ? AND status = 200 AND model IS NOT NULL
            GROUP BY model, provider ORDER BY sum(tokens_in) DESC LIMIT 40
        """, (since,)).fetchall()
        tot = sum(r["tin"] or 0 for r in rows) or 1
        for r in rows:
            data["models_24h"].append({
                "model": r["model"], "provider": r["provider"], "calls": r["n"],
                "tin": r["tin"] or 0, "tout": r["tout"] or 0,
                "avg_ctx": r["avg_ctx"] or 0,
                "pct": round(100.0 * (r["tin"] or 0) / tot, 1),
            })

        fails = o.execute("""
            SELECT status, count(*) n, sum(tokens_in) tin FROM call_logs
            WHERE timestamp >= ? AND status >= 400 GROUP BY status ORDER BY n DESC
        """, (since,)).fetchall()
        ok_tok = o.execute(
            "SELECT sum(tokens_in) FROM call_logs WHERE timestamp >= ? AND status=200",
            (since,)).fetchone()[0] or 0
        fail_tok = sum(r["tin"] or 0 for r in fails)
        data["failures"] = {
            "rows": [{"status": r["status"], "n": r["n"], "tin": r["tin"] or 0} for r in fails],
            "fail_tokens": fail_tok, "ok_tokens": ok_tok,
            "pct_of_success": round(100.0 * fail_tok / ok_tok, 4) if ok_tok else 0.0,
            "reasons": [{"status": r["status"], "why": _why("", "", r["status"])}
                        for r in fails],
        }

        # Same failures, but with the upstream's own classification (error_type)
        # and the model/provider that was hit — this is what tells you WHICH model
        # is dying and WHY, instead of a bare HTTP status.
        err_rows = o.execute("""
            SELECT coalesce(error_type, '(no type)') etype, coalesce(model, '?') model,
                   coalesce(provider, '?') provider, status, count(*) n,
                   sum(tokens_in) tin, max(timestamp) last
            FROM call_logs
            WHERE timestamp >= ? AND status >= 400
            GROUP BY etype, model, provider, status
            ORDER BY n DESC LIMIT 40
        """, (since,)).fetchall()
        data["provider_failures"] = [{
            "etype": r["etype"], "model": r["model"], "provider": r["provider"],
            "status": r["status"], "n": r["n"], "tin": r["tin"] or 0,
            "last": (r["last"] or "")[11:16],
            "why": _why(r["etype"], "", r["status"]),
        } for r in err_rows]

        recent = o.execute("""
            SELECT timestamp, status, model, provider, coalesce(error_type, '') etype,
                   coalesce(error_summary, '') esum, coalesce(combo_name, '') combo,
                   coalesce(combo_step_id, '') step, coalesce(session_tag, '') s_tag
            FROM call_logs WHERE timestamp >= ? AND status >= 400
            ORDER BY timestamp DESC LIMIT 60
        """, (since,)).fetchall()
        data["recent_provider_errors"] = [{
            "ts": (r["timestamp"] or "")[11:16], "status": r["status"],
            "model": r["model"] or "?", "provider": r["provider"] or "?",
            "etype": r["etype"],
            "summary": " ".join((r["esum"] or "").split())[:220],
            "tag": r["s_tag"], "combo": f"{r['combo']} {r['step']}".strip(),
            "why": _why(r["etype"], r["esum"], r["status"]),
        } for r in recent]

        if has_table(o, "combos"):
            for cb in o.execute("SELECT name, data FROM combos").fetchall():
                try:
                    models = json.loads(cb["data"]).get("models", [])
                except (ValueError, TypeError):
                    continue
                models.sort(key=lambda m: m.get("weight", 0))
                used = o.execute("""
                    SELECT model, count(*) n, sum(tokens_in) tin FROM call_logs
                    WHERE timestamp >= ? AND combo_name = ? AND status = 200
                    GROUP BY model
                """, (since, cb["name"])).fetchall()
                umap = {r["model"]: r for r in used}
                ctot = sum(r["tin"] or 0 for r in used) or 1

                def _key(s):
                    return str(s or "").lower().removeprefix("cfp/").removeprefix("cf/")

                steps = []
                for i, m in enumerate(models):
                    name = m.get("model") or "(unnamed)"
                    key = _key(name)
                    tail = key.split("/")[-1]
                    hit = next((r for k, r in umap.items()
                                if _key(k) == key or _key(k).endswith("/" + tail)), None)
                    steps.append({
                        "i": i, "model": name, "calls": hit["n"] if hit else 0,
                        "tin": hit["tin"] if hit else 0,
                        "pct": round(100.0 * (hit["tin"] or 0) / ctot, 1) if hit else 0.0,
                    })
                data["combos"].append({
                    "name": cb["name"], "steps": steps,
                    "pinned": bool(steps and steps[0]["pct"] > 90 and len(steps) > 1),
                    "pinned_pct": steps[0]["pct"] if steps else 0,
                })

    data["totals"] = {
        "input_tokens": sum(p["total_in"] for p in data["profiles"]),
        "calls": sum(p["total_calls"] for p in data["profiles"]),
        "window_input": sum(m["tin"] for m in data["models_24h"]),
        "live_sessions": len(data["live_sessions"]),
        "active_sessions": sum(1 for s in data["live_sessions"] if s["state"] == "active"),
        "sessions_with_fails": sum(1 for s in data["live_sessions"] if s["fails"]),
        "model_fails": logs["total"],
        "fallbacks": logs["fallbacks"],
        "provider_errors": sum(r["n"] for r in data["provider_failures"]),
        "gateway_state": data["gateway"].get("state") or "?",
        "top_reason": (logs["by_type"][0]["key"] + " — " + logs["by_type"][0]["why"]
                       if logs["by_type"] else ""),
    }
    return data



def cached(hours: float) -> dict:
    """build() memoised per window for CACHE_TTL seconds.

    Keyed by ``hours``: a single global slot made the 1h/6h/24h selector return
    whichever window happened to be built first inside the TTL.
    """
    with _lock:
        hit = _cache.get(hours)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
    data = build(hours)
    with _lock:
        _cache[hours] = (time.time(), data)
        if len(_cache) > 12:                 # bound it: the UI offers 5 windows
            for stale in sorted(_cache, key=lambda k: _cache[k][0])[:len(_cache) - 12]:
                _cache.pop(stale, None)
    return data


