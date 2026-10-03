"""usage-monitor agent half.

Provides:
  tools: usage_health, usage_combo_plan, usage_write_combo
  hook:  pre_llm_call  (log-warn when the routed model is rate-limited)
  cmd:   /usage        (health summary in any chat session)

Desktop UI lives in desktop/plugin.js; backend routes in dashboard/plugin_api.py.
All data reads are read-only; the ONLY write is the free-smart combo row.
"""

from __future__ import annotations

import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

_HEALTH_SCHEMA = {
    "name": "usage_health",
    "description": ("Model health summary: which models are rate-limited / "
                      "failing / healthy in the window. Read-only, no cost."),
    "parameters": {
        "type": "object",
        "properties": {
            "hours": {"type": "number", "description": "window hours"},
        },
    },
}

_PLAN_SCHEMA = {
    "name": "usage_combo_plan",
    "description": ("Dry-run plan of the smart free-model combo: deduped "
                      "order (healthy first, rate-limited last) + unused "
                      "healthy free models as candidates. Read-only, no write."),
    "parameters": {
        "type": "object",
        "properties": {
            "hours": {"type": "number", "description": "window hours"},
            "include_unused": {"type": "boolean",
                "description": "include unused healthy free models"},
        },
    },
}

_WRITE_SCHEMA = {
    "name": "usage_write_combo",
    "description": ("Write the smart free-model plan into the OmniRoute combo "
                      "'free-smart' (upserts ONLY that row; never touches other "
                      "combos). Run usage_combo_plan first to review."),
    "parameters": {
        "type": "object",
        "properties": {
            "hours": {"type": "number", "description": "window hours"},
            "include_unused": {"type": "boolean",
                "description": "include unused healthy free models"},
        },
    },
}


def _api():
    """Lazy-import dashboard/plugin_api.py (adds its dir to sys.path once)."""
    dash = os.path.join(os.path.dirname(__file__), "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import plugin_api
    return plugin_api


def _handle_health(hours=24.0):
    api = _api()
    h = api.model_health(float(hours or 24.0))
    lines = ["window %sh: %s" % (h["window_hours"], h["summary"])]
    for m in h["models"][:25]:
        lines.append("%s | %s | %s ok=%d err=%d (%s%%) %s" % (
            m["status"], m["model"][:55], m["provider"][:30],
            m["ok_calls"], m["err_calls"], m["err_rate"],
            (m["last_why"] or "")[:70]))
    return "\n".join(lines)


def _format_plan(plan):
    lines = ["free-smart plan — window %sh, pool %d, candidates %d, "
             "include_unused=%s" % (plan["window_hours"], plan["pool_size"],
                                     len(plan.get("candidates", [])),
                                     plan.get("include_unused", False))]
    for s in plan["steps"][:80]:
        tag = " [unused]" if s.get("source") == "unused" else ""
        lines.append("#%-3d %-9s %s @ %s%s" % (
            s["i"], s["status"], s["model"][:55], s["providerId"][:28], tag))
    if len(plan["steps"]) > 80:
        lines.append("... %d more" % (len(plan["steps"]) - 80))
    return "\n".join(lines)


def _handle_plan(hours=24.0, include_unused=False):
    api = _api()
    return _format_plan(api.smart_combo(float(hours or 24.0),
                                        bool(include_unused)))


def _handle_write(hours=24.0, include_unused=False):
    api = _api()
    hours = float(hours or 24.0)
    plan = api.smart_combo(hours, bool(include_unused))
    if not plan["steps"]:
        return "refused: empty pool — nothing to write"
    written_at = api._write_combo(plan)
    return ("OK: free-smart rewritten at %s with %d models "
            "(include_unused=%s)" %
            (written_at, len(plan["steps"]), bool(include_unused)))


# ── /usage slash command (CLI + Telegram/Discord sessions) ───────────────
def _cmd_usage(raw_args: str) -> str:
    hours = 24.0
    parts = (raw_args or "").strip().split()
    if parts:
        try:
            hours = max(0.1, min(720.0, float(parts[0].rstrip("hH"))))
        except ValueError:
            pass
    try:
        api = _api()
        h = api.model_health(hours)
        s = h["summary"]
        lines = ["usage-monitor %gh: limited=%d failing=%d degraded=%d "
                 "healthy=%d" % (hours, s["limited"], s["failing"],
                                 s["degraded"], s["healthy"])]
        tr = h.get("trend") or {}
        if tr.get("errors_delta_pct") is not None:
            lines.append("errors %d vs prev %d (%s%%)" % (
                tr.get("errors_now", 0), tr.get("errors_prev", 0),
                tr.get("errors_delta_pct")))
        limited = [m for m in h["models"] if m["status"] == "limited"]
        for m in limited[:8]:
            lines.append("LIMITED %s @ %s — %s" % (
                m["model"][:50], m["provider"][:24],
                (m["last_why"] or "")[:70]))
        for p in api._combos_pinned(hours)[:5]:
            lines.append("PINNED combo %s: %s%% on %s" % (
                p["name"], p["pct"], p["first_model"][:50]))
        fb = api._fallback_chain(hours)
        if fb.get("total"):
            lines.append("fallback: %d/%d unhealthy" % (
                fb["unhealthy"], fb["total"]))
        return "\n".join(lines)
    except Exception as exc:
        return "usage-monitor error: %s" % exc


# ── pre_llm_call: warn (never block) when the routed model is throttled ───
_last_warn: dict = {}


def _hook_pre_llm_call(**kwargs):
    model = str(kwargs.get("model") or "")
    if not model:
        return None
    now = time.time()
    if now - _last_warn.get(model, 0) < 600:
        return None           # one log line per model per 10 minutes
    try:
        api = _api()
        core = api.core
        c = core.ro(core.OMNI_DB)
        if c is None:
            return None
        try:
            since = core.since_iso(0.25)      # last 15 minutes
            bare = model.split("/")[-1]
            row = c.execute(
                "SELECT count(*) FROM call_logs WHERE timestamp >= ?"
                " AND status = 429 AND (model = ? OR model LIKE ?"
                " OR model LIKE ?)",
                (since, model, "%/" + bare, "%" + bare)).fetchone()
            n429 = int(row[0] or 0) if row else 0
        finally:
            try:
                c.close()
            except Exception:
                pass
        if n429 > 0:
            _last_warn[model] = now
            logger.warning(
                "usage-monitor: model %s got %d rate-limit (429) answers in "
                "the last 15m — consider a different model or waiting out the "
                "window", model, n429)
    except Exception:
        logger.debug("usage-monitor: pre_llm_call health probe failed",
                     exc_info=True)
    return None                # observer: never injects, never blocks


def register(ctx):
    ctx.register_tool(name="usage_health", toolset="usage",
                      schema=_HEALTH_SCHEMA, handler=_handle_health)
    ctx.register_tool(name="usage_combo_plan", toolset="usage",
                      schema=_PLAN_SCHEMA, handler=_handle_plan)
    ctx.register_tool(name="usage_write_combo", toolset="usage",
                      schema=_WRITE_SCHEMA, handler=_handle_write)
    ctx.register_hook("pre_llm_call", _hook_pre_llm_call)
    ctx.register_command("usage", handler=_cmd_usage,
                         description="Model/rate-limit health summary (usage-monitor)")
