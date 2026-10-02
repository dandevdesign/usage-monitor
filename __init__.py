"""usage-monitor agent half — minimal register() so plugins.enable works.

The real work lives in dashboard/plugin_api.py (backend routes) and
desktop/plugin.js (Desktop UI). This stub exposes one tool so the
plugin is a valid native plugin: usage_health.
"""

SCHEMA = {
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


def _handle(hours=24.0):
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "dashboard"))
    import plugin_api as _api
    h = _api.model_health(float(hours or 24.0))
    lines = ["window %sh: %s" % (h["window_hours"], h["summary"])]
    for m in h["models"][:25]:
        lines.append("%s | %s | %s ok=%d err=%d (%s%%) %s" % (
            m["status"], m["model"][:55], m["provider"][:30],
            m["ok_calls"], m["err_calls"], m["err_rate"],
            (m["last_why"] or "")[:70]))
    return "\n".join(lines)


def register(ctx):
    ctx.register_tool(name="usage_health", toolset="usage",
                      schema=SCHEMA, handler=_handle)
