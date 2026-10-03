"""usage-monitor backend — dashboard + free-model health + smart combo.

Mounted at /api/plugins/usage-monitor/. ctx.rest('/data?hours=24')
from desktop plugin.js reaches the same routes. Read-only except
POST /combo/write which upserts ONE combo row (free-smart).
No external API calls, no cost.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import usage_core as core

router = APIRouter()
PLUGIN_VERSION = "1.1.0"

_HEALTH_TTL = 10.0
_health_cache: dict = {}


def _omni():
    c = core.ro(core.OMNI_DB)
    if c is None:
        raise HTTPException(status_code=503, detail="omniroute storage missing")
    return c


def _norm_model(name):
    s = str(name or "").strip()
    s = re.sub(r"^(cfp/|cf/|kc/|Merge/)", "", s, flags=re.I)
    s = re.sub(r"^@cf/", "", s)
    s = s.split("/")[-1] if "/" in s else s
    return s.lower()


def _is_free_entry(model, provider_id):
    m = str(model or "")
    p = str(provider_id or "").lower()
    if ":free" in m.lower() or "-free" in m.lower():
        return True
    return p in ("kilocode", "openrouter", "nous-research", "cloudflare-ai",
                 "cloudflare-playground", "agentrouter", "aihorde", "cline",
                 "experientiallabs", "ollama", "opencode-zen", "opencode")


def _parse_iso_ts(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _quota_latest(o):
    """Newest quota_snapshots row per provider (OmniRoute quota telemetry)."""
    if not core.has_table(o, "quota_snapshots"):
        return []
    rows = o.execute("SELECT provider, connection_id, remaining_percentage," +
        " is_exhausted, next_reset_at, created_at FROM quota_snapshots" +
        " ORDER BY created_at DESC LIMIT 500").fetchall()
    out = {}
    for r in rows:
        prov = str(r["provider"] or "?")
        if prov in out:
            continue          # newest-first, first row wins
        out[prov] = {"provider": prov,
            "connection_id": r["connection_id"] or "",
            "remaining_pct": r["remaining_percentage"],
            "exhausted": bool(r["is_exhausted"]),
            "next_reset_at": str(r["next_reset_at"] or "")[:19].replace("T", " "),
            "created_at": str(r["created_at"] or "")[:19]}
    return list(out.values())


def _trend(o, hours):
    """Current window vs the immediately-preceding window (delta %)."""
    if not core.has_table(o, "call_logs"):
        return {}
    since = core.since_iso(hours)
    prev_since = core.since_iso(hours * 2)
    cur = o.execute("SELECT sum(status = 200), sum(status >= 400)" +
        " FROM call_logs WHERE timestamp >= ?", (since,)).fetchone()
    prev = o.execute("SELECT sum(status = 200), sum(status >= 400)" +
        " FROM call_logs WHERE timestamp >= ? AND timestamp < ?",
        (prev_since, since)).fetchone()

    def _delta(a, b):
        a, b = int(a or 0), int(b or 0)
        if b == 0:
            return None if a == 0 else 100.0
        return round(100.0 * (a - b) / b, 1)
    return {
        "errors_now": int(cur[1] or 0), "errors_prev": int(prev[1] or 0),
        "errors_delta_pct": _delta(cur[1], prev[1]),
        "ok_now": int(cur[0] or 0), "ok_prev": int(prev[0] or 0),
        "ok_delta_pct": _delta(cur[0], prev[0]),
        "window_hours": hours}


def _err_timeline(o, hours=30.0):
    """Hourly error counts, last 30h, for the top-40 erroring model@provider."""
    if not core.has_table(o, "call_logs"):
        return {}
    since = core.since_iso(hours)
    rows = o.execute("SELECT coalesce(model,'?') m, coalesce(provider,'?') p," +
        " substr(timestamp, 1, 13) h, count(*) n FROM call_logs" +
        " WHERE timestamp >= ? AND status >= 400 GROUP BY m, p, h",
        (since,)).fetchall()
    per, totals = {}, {}
    for r in rows:
        k = (r["m"], r["p"])
        per.setdefault(k, {})[r["h"]] = r["n"]
        totals[k] = totals.get(k, 0) + (r["n"] or 0)
    top = sorted(totals, key=lambda k: -totals[k])[:40]
    buckets = [(datetime.now(timezone.utc) - timedelta(hours=i)).
               strftime("%Y-%m-%dT%H") for i in range(29, -1, -1)]
    series = {k[0] + "|" + k[1]: [per[k].get(b, 0) for b in buckets]
              for k in top}
    return {"buckets": buckets, "series": series}


def _combos_pinned(hours=24.0):
    """Combos stuck >90% on their first step (from the cached dashboard)."""
    try:
        data = core.cached(hours)
    except Exception:
        return []
    out = []
    for cb in data.get("combos", []):
        if cb.get("pinned"):
            steps = cb.get("steps") or []
            out.append({"name": cb["name"], "pct": cb.get("pinned_pct", 0),
                        "first_model": steps[0].get("model", "") if steps else ""})
    return out


def _fallback_chain(hours=24.0):
    """Hermes fallback_providers from config, annotated with live health."""
    entries = []
    try:
        from hermes_constants import get_hermes_home
        cfg = get_hermes_home() / "config.yaml"
        if cfg.exists():
            import yaml
            parsed = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            entries = parsed.get("fallback_providers") or []
    except Exception:
        entries = []
    h = model_health(hours)
    by_key = {}
    for m in h["models"]:
        by_key[(m["model"].lower(), str(m["provider"]).lower())] = m
        by_key.setdefault((_norm_model(m["model"]), str(m["provider"]).lower()), m)
    by_base = {}
    for m in h["models"]:
        by_base.setdefault(_norm_model(m["model"]), m)
    chain = []
    for e in entries:
        if isinstance(e, str):
            prov, model = "", e
        elif isinstance(e, dict):
            model = str(e.get("model") or e.get("name") or "")
            prov = str(e.get("provider") or e.get("providerId") or "")
        else:
            continue
        hit = None
        if model:
            hit = (by_key.get((model.lower(), prov.lower())) or
                   by_key.get((_norm_model(model), prov.lower())) or
                   by_base.get(_norm_model(model)))
        chain.append({"model": model, "provider": prov,
                      "status": hit["status"] if hit else "unknown",
                      "why": (hit or {}).get("last_why", "")})
    bad = sum(1 for c in chain if c["status"] in ("limited", "failing"))
    return {"chain": chain, "total": len(chain), "unhealthy": bad}


_last_broadcast = {"digest": None}


def _maybe_broadcast(summary, pinned):
    """Push health.changed to Desktop windows on status/pin changes."""
    digest = json.dumps([summary, sorted(p["name"] for p in pinned)],
                        sort_keys=True)
    if _last_broadcast["digest"] == digest:
        return
    _last_broadcast["digest"] = digest
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event
        broadcast_plugin_event("usage-monitor", "health.changed",
                               {"summary": summary,
                                "pinned": [p["name"] for p in pinned]})
    except Exception:
        pass      # plugin-only process without a gateway: silent no-op


def _iso(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _latency_map(o, hours):
    """avg latency + ttft per model@provider from usage_history (window)."""
    if not core.has_table(o, "usage_history"):
        return {}
    rows = o.execute("SELECT coalesce(model,'?') m, coalesce(provider,'?') p," +
        " round(avg(latency_ms)) lat, round(avg(ttft_ms)) ttft" +
        " FROM usage_history WHERE timestamp >= ? AND success = 1" +
        " AND latency_ms IS NOT NULL GROUP BY m, p",
        (core.since_iso(hours),)).fetchall()
    return {(r["m"], r["p"]): {"lat": r["lat"], "ttft": r["ttft"]}
            for r in rows}


def _context_maps(o):
    """context windows: (by provider+model_id, by normalized model id)."""
    if not core.has_table(o, "model_context_overrides"):
        return {}, {}
    rows = o.execute("SELECT provider, model_id, real_context" +
        " FROM model_context_overrides").fetchall()
    by_prov, norm = {}, {}
    for r in rows:
        prov = str(r["provider"] or "")
        mid = str(r["model_id"] or "").lower()
        if not mid or not r["real_context"]:
            continue
        by_prov.setdefault(prov, {})[mid] = r["real_context"]
        norm.setdefault(_norm_model(mid), r["real_context"])
    return by_prov, norm


def _lookup_ctx(maps, model, provider):
    by_prov, norm = maps
    pm = by_prov.get(str(provider), {})
    v = pm.get(str(model or "").lower())
    if v is None:
        v = pm.get(_norm_model(model))
    if v is None:
        v = norm.get(_norm_model(model))
    return v


def _intel_map(o):
    """best intelligence score per normalized model (0..1)."""
    if not core.has_table(o, "model_intelligence"):
        return {}
    rows = o.execute("SELECT model, score FROM model_intelligence" +
        " WHERE (expires_at IS NULL OR expires_at > datetime('now'))"
        ).fetchall()
    out = {}
    for r in rows:
        n = _norm_model(r["model"])
        sc = float(r["score"] or 0)
        if n not in out or sc > out[n]:
            out[n] = sc
    return out


def _breakers(o):
    """domain circuit breakers: full list + provider -> state map."""
    if not core.has_table(o, "domain_circuit_breakers"):
        return [], {}
    rows = o.execute("SELECT name, state, failure_count," +
        " last_failure_time FROM domain_circuit_breakers").fetchall()
    lst = [{"name": r["name"], "state": r["state"],
            "failures": r["failure_count"] or 0,
            "last_failure": core._ago(r["last_failure_time"])
                if r["last_failure_time"] else ""} for r in rows]
    return lst, {str(r["name"]): str(r["state"]) for r in rows}


def _reset_cadence(o):
    """per provider+window: typical reset duration and next predicted reset."""
    if not core.has_table(o, "provider_quota_reset_events"):
        return {}
    rows = o.execute("SELECT provider, window_key, window_started_at," +
        " window_resets_at FROM provider_quota_reset_events" +
        " ORDER BY id DESC LIMIT 200").fetchall()
    groups = {}
    for r in rows:
        st, rs = _iso(r["window_started_at"]), _iso(r["window_resets_at"])
        if st and rs:
            groups.setdefault((str(r["provider"]), str(r["window_key"])),
                              []).append((st, rs))
    out = {}
    now = datetime.now(timezone.utc)
    for (prov, wk), pairs in groups.items():
        durs = [(rs - st).total_seconds() / 3600.0 for st, rs in pairs
                if rs > st]
        if not durs:
            continue
        cad = sum(durs) / len(durs)
        nxt = max(rs for st, rs in pairs)
        guard = 0
        while nxt <= now and guard < 400:
            nxt = nxt + timedelta(hours=cad)
            guard += 1
        if nxt <= now:
            continue
        out.setdefault(prov, []).append({
            "window_key": wk, "cadence_h": round(cad, 1),
            "next_est": nxt.strftime("%Y-%m-%d %H:%M"),
            "countdown_h": round((nxt - now).total_seconds() / 3600.0, 1)})
    for v in out.values():
        v.sort(key=lambda x: x["countdown_h"])
    return out

def model_health(hours=24.0):
    now = time.time()
    ck = round(hours, 2)
    hit = _health_cache.get(ck)
    if hit and now - hit[0] < _HEALTH_TTL:
        return hit[1]
    since = core.since_iso(hours)
    o = _omni()
    try:
        has_cl = core.has_table(o, "call_logs")
        has_pc = core.has_table(o, "provider_connections")
        ok_rows = o.execute("SELECT coalesce(model,'?') model," +
            " coalesce(provider,'?') provider, count(*) n," +
            " sum(tokens_in) tin, max(timestamp) last_ok FROM call_logs" +
            " WHERE timestamp >= ? AND status = 200" +
            " GROUP BY model, provider",
            (since,)).fetchall() if has_cl else []
        err_rows = o.execute("SELECT coalesce(model,'?') model," +
            " coalesce(provider,'?') provider, coalesce(error_type,'') etype," +
            " status, count(*) n, max(timestamp) last_err," +
            " coalesce(error_summary,'') esum FROM call_logs" +
            " WHERE timestamp >= ? AND status >= 400" +
            " GROUP BY model, provider, etype, status" +
            " ORDER BY n DESC LIMIT 500",
            (since,)).fetchall() if has_cl else []
        conns = o.execute("SELECT provider, is_active, rate_limited_until," +
            " coalesce(last_error,'') last_error FROM provider_connections"
            ).fetchall() if has_pc else []
        quotas = _quota_latest(o)
        trend = _trend(o, hours)
        timeline = _err_timeline(o)
        lat = _latency_map(o, hours)
        ctx_maps = _context_maps(o)
        intel = _intel_map(o)
        breaker_list, breaker_map = _breakers(o)
        cadence = _reset_cadence(o)
    finally:
        try:
            o.close()
        except Exception:
            pass
    ok_map = {}
    for r in ok_rows:
        ok_map[(r["model"], r["provider"])] = {"ok_calls": r["n"],
            "ok_tokens": r["tin"] or 0, "last_ok": (r["last_ok"] or "")[5:16]}
    err_map = {}
    for r in err_rows:
        key = (r["model"], r["provider"])
        e = err_map.setdefault(key, {"err_calls": 0, "errors": [],
            "last_err": "", "last_status": None, "last_why": "",
            "last_err_iso": ""})
        e["err_calls"] += r["n"] or 0
        why = core._why(r["etype"], r["esum"], r["status"])
        e["errors"].append({"etype": r["etype"] or "(no type)",
            "status": r["status"], "n": r["n"], "why": why,
            "raw": (r["esum"] or "")[:600]})
        if (r["last_err"] or "") > e["last_err_iso"]:
            e["last_err_iso"] = r["last_err"] or ""
            e["last_err"] = (r["last_err"] or "")[5:16]
            e["last_status"] = r["status"]
            e["last_why"] = why
    thro = {}
    for c in conns:
        rl = str(c["rate_limited_until"] or "")
        try:
            rl_ts = datetime.fromisoformat(
                rl.replace("Z", "+00:00")).timestamp() if rl else 0
        except (ValueError, TypeError):
            rl_ts = 0
        prev = thro.get(str(c["provider"]), {})
        thro[str(c["provider"])] = {
            "rate_limited_until": rl or prev.get("rate_limited_until", ""),
            "rate_limited": (rl_ts > now) or prev.get("rate_limited", False),
            "active": bool(c["is_active"]) and prev.get("active", True),
            "last_error": (c["last_error"] or prev.get("last_error", ""))[:200]}
    for q in quotas:
        q["predicted"] = cadence.get(q["provider"], [])[:3]
    quota_map = {q["provider"]: q for q in quotas}
    models = []
    for model, provider in set(ok_map) | set(err_map):
        ok = ok_map.get((model, provider), {})
        er = err_map.get((model, provider), {})
        ok_n = ok.get("ok_calls", 0)
        err_n = er.get("err_calls", 0)
        tot = ok_n + err_n
        err_rate = round(100.0 * err_n / tot, 1) if tot else 0.0
        rl_n = sum(e["n"] for e in er.get("errors", [])
                   if "rate limit" in e["why"] or e["status"] == 429)
        ps = thro.get(str(provider), {})
        quota = quota_map.get(str(provider))
        last_err_ts = _parse_iso_ts(er.get("last_err_iso"))
        recent_err = bool(last_err_ts) and (now - last_err_ts) < 900
        # auto-recovery: a 429 whose reset has passed (no fresh error in 15m)
        # demotes back to degraded instead of staying limited all window.
        rl_recent = rl_n > 0 and err_rate >= 80 and ok_n == 0 and recent_err
        q_pct = (quota or {}).get("remaining_pct")
        q_exhausted = bool(quota and quota.get("exhausted"))
        if ps.get("rate_limited") or rl_recent or q_exhausted:
            status = "limited"
        elif err_rate >= 50 or not ps.get("active", True) or (q_pct is not None and q_pct < 10):
            status = "failing"
        elif (rl_n > 0 or err_rate >= 10 or ps.get("last_error")
              or (q_pct is not None and q_pct < 25)):
            status = "degraded"
        else:
            status = "healthy"
        models.append({
            "model": model, "provider": provider,
            "base": _norm_model(model),
            "free": (":free" in str(model).lower()
                       or "-free" in str(model).lower()),
            "ok_calls": ok_n, "ok_tokens": ok.get("ok_tokens", 0),
            "err_calls": err_n, "err_rate": err_rate,
            "rate_limited_calls": rl_n,
            "errors": sorted(er.get("errors", []),
                             key=lambda e: -e["n"])[:4],
            "last_ok": ok.get("last_ok", ""),
            "last_err": er.get("last_err", ""),
            "last_status": er.get("last_status"),
            "last_why": er.get("last_why", "") or ps.get("last_error", ""),
            "quota_pct": (quota or {}).get("remaining_pct"),
            "avg_latency_ms": (lat.get((model, provider)) or {}).get("lat"),
            "avg_ttft_ms": (lat.get((model, provider)) or {}).get("ttft"),
            "ctx_real": _lookup_ctx(ctx_maps, model, provider),
            "iq": round(100 * intel[_norm_model(model)])
                if _norm_model(model) in intel else None,
            "conn_rate_limited": ps.get("rate_limited", False),
            "conn_rate_limited_until": ps.get("rate_limited_until", ""),
            "conn_active": ps.get("active", True),
            "status": status})
        st = breaker_map.get(str(provider))
        if st and st != "CLOSED":
            models[-1]["status"] = "failing"
            models[-1]["last_why"] = "circuit breaker " + st
    models.sort(key=lambda m: ({"limited": 0, "failing": 1,
        "degraded": 2, "healthy": 3}[m["status"]], -m["err_calls"]))
    summary = {"limited": 0, "failing": 0, "degraded": 0, "healthy": 0}
    for m in models:
        summary[m["status"]] += 1
    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "window_hours": hours, "summary": summary, "models": models,
               "throttled_providers": thro, "quota": quotas,
               "trend": trend, "err_timeline": timeline,
               "circuit_breakers": breaker_list,
               "has_intel": bool(intel)}
    _health_cache[ck] = (now, payload)
    return payload

def smart_combo(hours=24.0, include_unused=False):
    health = model_health(hours)
    hmap = {(m["model"], m["provider"]): m for m in health["models"]}
    o = _omni()
    try:
        has_cb = core.has_table(o, "combos")
        rows = o.execute(
            "SELECT name, data FROM combos").fetchall() if has_cb else []
    finally:
        try:
            o.close()
        except Exception:
            pass
    pool = {}
    for cb in rows:
        try:
            ms = json.loads(cb["data"]).get("models", [])
        except (ValueError, TypeError):
            continue
        for m in ms:
            name = m.get("model") or ""
            pid = m.get("providerId") or ""
            if not name or not _is_free_entry(name, pid):
                continue
            base = _norm_model(name)
            if not base or base == "connection-test":
                continue
            cand_h = hmap.get((name, pid), {})
            cand_rank = {"healthy": 0, "degraded": 1, "failing": 2,
                         "limited": 3}.get(cand_h.get("status"), 1)
            cand_tok = cand_h.get("ok_tokens", 0)
            cur = pool.get(base)
            if cur is None or (cand_rank, -cand_tok) < (cur["_rank"],
                                                        -cur["_tok"]):
                pool[base] = {"model": name, "providerId": pid,
                              "source": cb["name"], "_rank": cand_rank,
                              "_tok": cand_tok}
    # unused healthy free models — candidates not yet in ANY combo (#5)
    pool_bases = set(pool)
    candidates = []
    for m in health["models"]:
        if m["status"] != "healthy":
            continue
        if not (m.get("free") or _is_free_entry(m["model"], m["provider"])):
            continue
        if m["base"] in pool_bases:
            continue
        candidates.append({"model": m["model"], "provider": m["provider"],
            "ok_tokens": m.get("ok_tokens", 0),
            "ctx_real": m.get("ctx_real"),
            "avg_latency_ms": m.get("avg_latency_ms")})
    candidates.sort(key=lambda c: -c["ok_tokens"])
    if include_unused:
        for c in candidates[:40]:
            pool[_norm_model(c["model"])] = {
                "model": c["model"], "providerId": c["provider"],
                "source": "unused", "_rank": 0, "_tok": c["ok_tokens"]}
    ordered = sorted(pool.values(),
                     key=lambda e: (e["_rank"], -e["_tok"], e["model"]))
    steps = []
    for i, e in enumerate(ordered):
        h = hmap.get((e["model"], e["providerId"]), {})
        status = h.get("status", "healthy")
        steps.append({
            "i": i, "model": e["model"], "providerId": e["providerId"],
            "source": e.get("source", "combo"),
            "status": status, "ok_calls": h.get("ok_calls", 0),
            "err_calls": h.get("err_calls", 0),
            "err_rate": h.get("err_rate", 0.0),
            "last_why": h.get("last_why", ""),
            "why_placed": ("top: healthy, answered recently"
                            if status == "healthy"
                            else "demoted: " + h.get("last_why", status))})
    return {"generated_at": datetime.now().isoformat(timespec="seconds"),
            "window_hours": hours, "combo_name": "free-smart",
            "pool_size": len(pool), "steps": steps,
            "candidates": candidates[:40],
            "include_unused": bool(include_unused),
            "note": "1 entry per model; healthy first, rate-limited last"}


@router.get("/data")
def api_data(hours: float = 24.0):
    hours = max(0.1, min(720.0, hours))
    payload = core.cached(hours)
    try:
        payload["velocity"] = _velocity()
    except Exception:
        payload.setdefault("velocity", [])
    return JSONResponse(payload)


@router.get("/health")
def api_health(hours: float = 24.0):
    hours = max(0.1, min(720.0, hours))
    return JSONResponse(model_health(hours))


@router.get("/summary")
def api_summary(hours: float = 24.0):
    hours = max(0.1, min(720.0, hours))
    h = model_health(hours)
    pinned = _combos_pinned(hours)
    fb = _fallback_chain(hours)
    _maybe_broadcast(h["summary"], pinned)
    limited_models = [{"model": m["model"], "provider": m["provider"]}
                      for m in h["models"] if m["status"] == "limited"][:30]
    return JSONResponse({"generated_at": h["generated_at"],
                         "summary": h["summary"], "pinned": pinned,
                         "fallback": fb, "limited_models": limited_models})


@router.get("/combo")
def api_combo(hours: float = 24.0, include_unused: int = 0):
    hours = max(0.1, min(720.0, hours))
    return JSONResponse(smart_combo(hours, bool(include_unused)))


@router.get("/version")
def api_version():
    return {"name": "usage-monitor", "version": PLUGIN_VERSION}


def _velocity(mins=5.0):
    """Top token burners in the last N minutes (OmniRoute session_tag)."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=mins)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    c = core.ro(core.OMNI_DB)
    if c is None or not core.has_table(c, "call_logs"):
        return []
    try:
        rows = c.execute("SELECT session_tag, coalesce(model,'?') model," +
            " count(*) n, sum(tokens_in + tokens_out) tok FROM call_logs" +
            " WHERE timestamp >= ? AND session_tag IS NOT NULL" +
            " AND session_tag != '' GROUP BY session_tag, model",
            (since,)).fetchall()
    finally:
        try:
            c.close()
        except Exception:
            pass
    agg = {}
    for r in rows:
        a = agg.setdefault(r["session_tag"], {"tag": r["session_tag"],
            "calls": 0, "tok": 0, "model": r["model"]})
        a["calls"] += r["n"] or 0
        a["tok"] += r["tok"] or 0
        if (r["tok"] or 0) > 0:
            a["model"] = r["model"]
    out = []
    for a in agg.values():
        a["tpm"] = int(round(a["tok"] / mins))
        out.append(a)
    out.sort(key=lambda a: -a["tpm"])
    return out[:10]


def _combo_digest(steps):
    h = hashlib.sha1()
    for s in steps:
        h.update((str(s["model"]) + "@" + str(s["providerId"]) + ";").
                 encode("utf-8", "replace"))
    return h.hexdigest()


def _current_combo_digest():
    """Digest of the stored free-smart combo, or None when absent."""
    c = core.ro(core.OMNI_DB)
    if c is None or not core.has_table(c, "combos"):
        return None
    try:
        row = c.execute("SELECT data FROM combos WHERE name='free-smart'"
                        ).fetchone()
    finally:
        try:
            c.close()
        except Exception:
            pass
    if not row:
        return None
    try:
        models = json.loads(row["data"]).get("models", [])
    except (ValueError, TypeError, AttributeError):
        return None
    return _combo_digest([{"model": m.get("model"),
                           "providerId": m.get("providerId")}
                          for m in models])


def _write_combo(plan):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = json.dumps({
        "name": "free-smart", "strategy": "priority",
        "config": {"maxRetries": 2, "retryDelayMs": 1000},
        "models": [{"id": "combo-model-%d-free-smart" % s["i"],
                    "kind": "model", "model": s["model"],
                    "providerId": s["providerId"], "weight": s["i"]}
                   for s in plan["steps"]]})
    rw = sqlite3.connect(core.OMNI_DB, timeout=10)
    try:
        row = rw.execute(
            "SELECT id FROM combos WHERE name='free-smart'").fetchone()
        if row:
            rw.execute("UPDATE combos SET data=?, updated_at=?" +
                       " WHERE name='free-smart'", (data, now))
        else:
            rw.execute("INSERT INTO combos(id,name,data,sort_order," +
                       "created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (str(uuid.uuid4()), "free-smart", data, 99, now, now))
        rw.commit()
    finally:
        rw.close()
    return now


@router.post("/combo/write")
def api_combo_write(body: dict | None = None):
    body = body or {}
    hours = max(0.1, min(720.0, float(body.get("hours", 24.0))))
    plan = smart_combo(hours, bool(body.get("include_unused")))
    if not plan["steps"]:
        raise HTTPException(status_code=409, detail="empty pool")
    written_at = _write_combo(plan)
    return {"ok": True, "combo": "free-smart",
            "steps": len(plan["steps"]), "written_at": written_at,
            "generated_at": plan["generated_at"]}


@router.post("/combo/auto-refresh")
def api_combo_auto(body: dict | None = None):
    """Recompute and rewrite free-smart ONLY when the order changed (#6)."""
    body = body or {}
    hours = max(0.1, min(720.0, float(body.get("hours", 24.0))))
    plan = smart_combo(hours, bool(body.get("include_unused")))
    if not plan["steps"]:
        raise HTTPException(status_code=409, detail="empty pool")
    new_d = _combo_digest(plan["steps"])
    if _current_combo_digest() == new_d:
        return {"ok": True, "changed": False, "steps": len(plan["steps"]),
                "checked_at": datetime.now().isoformat(timespec="seconds")}
    written_at = _write_combo(plan)
    return {"ok": True, "changed": True, "steps": len(plan["steps"]),
            "written_at": written_at,
            "checked_at": datetime.now().isoformat(timespec="seconds")}
