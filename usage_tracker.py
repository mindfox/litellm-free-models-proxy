#!/usr/bin/env python3
"""
Usage tracker for litellm-free-models-proxy.

Calls the LiteLLM proxy's /usage endpoint (and /model/info) to get per-model
and per-provider token usage, then writes docs/usage.json.

Run alongside probe_models.py — this gives you the "how much is left on the
free tier" view on top of the availability data.

Usage:
    python usage_tracker.py

Environment variables:
    LITELLM_BASE_URL  — LiteLLM proxy base URL  (default: http://litellm:4000)
    LITELLM_MASTER_KEY — LiteLLM master key
"""

import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).parent
OUT_JSON = ROOT / "docs" / "usage.json"

LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")

_TIMEOUT = 15


def _get(path, params=None):
    """GET against LiteLLM endpoint, returns parsed JSON."""
    url = f"{LITELLM_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {LITELLM_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"GET {url} → {e.code} {body[:200]}")
    except Exception as e:
        raise RuntimeError(f"GET {url} → {e}")


def fetch_usage(since_hours=24):
    """
    Fetch /usage from LiteLLM for the last `since_hours`.
    Returns dict: {model_id: {"input_tokens": int, "output_tokens": int, "total_tokens": int, "num_requests": int}}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    params = {
        "start_date": cutoff.isoformat(),
        "end_date": datetime.now(timezone.utc).isoformat(),
    }
    try:
        data = _get("/usage", params=params)
    except Exception as e:
        print(f"[usage] /usage fetch failed: {e}")
        return {}

    # LiteLLM /usage returns {"data": [{...usage_records...}]}
    # Each record has: model, input_tokens, output_tokens, total_tokens, num_requests, start_time, end_time
    by_model = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "num_requests": 0})
    for record in data.get("data", []):
        model = record.get("model", "")
        if not model:
            continue
        by_model[model]["input_tokens"] += record.get("input_tokens", 0)
        by_model[model]["output_tokens"] += record.get("output_tokens", 0)
        by_model[model]["total_tokens"] += record.get("total_tokens", 0)
        by_model[model]["num_requests"] += record.get("num_requests", 0)

    return dict(by_model)


def fetch_spend(since_hours=24):
    """
    Fetch spend per model from LiteLLM /spent.
    Returns dict: {model_id: {"spend": float, "total_tokens": int}}
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    params = {
        "start_date": cutoff.isoformat(),
        "end_date": datetime.now(timezone.utc).isoformat(),
    }
    try:
        data = _get("/spent", params=params)
    except Exception as e:
        print(f"[usage] /spent fetch failed: {e}")
        return {}

    by_model = defaultdict(lambda: {"spend": 0.0, "total_tokens": 0})
    for record in data.get("data", []):
        model = record.get("model", "")
        if not model:
            continue
        by_model[model]["spend"] += record.get("spend", 0.0)
        by_model[model]["total_tokens"] += record.get("total_tokens", 0)

    return dict(by_model)


def fetch_models():
    """Fetch litellm model list via /model/info."""
    try:
        data = _get("/model/info")
        result = {}
        for entry in data.get("data", []):
            lp = entry.get("litellm_params", {})
            model_str = lp.get("model", "")
            if model_str:
                result[model_str] = {
                    "id": entry.get("model_info", {}).get("id", ""),
                    "model_name": entry.get("model_name", ""),
                    "litellm_model": model_str,
                }
        return result
    except Exception as e:
        print(f"[usage] /model/info fetch failed: {e}")
        return {}


def load_availability():
    """Load docs/availability.json for observed rate-limit data."""
    avail_path = ROOT / "docs" / "availability.json"
    if not avail_path.exists():
        return {}
    try:
        return json.loads(avail_path.read_text())
    except Exception:
        return {}


def main():
    print(f"[usage] Fetching LiteLLM usage data...")
    print(f"[usage] LITELLM_BASE={LITELLM_BASE}")

    models = fetch_models()
    usage_24h = fetch_usage(since_hours=24)
    usage_7d = fetch_usage(since_hours=168)
    spend_7d = fetch_spend(since_hours=168)
    avail = load_availability()

    # Build per-model breakdown
    model_rows = []
    all_model_ids = set(usage_24h.keys()) | set(usage_7d.keys()) | set(spend_7d.keys())

    for mid in sorted(all_model_ids):
        u24 = usage_24h.get(mid, {})
        u7 = usage_7d.get(mid, {})
        sp = spend_7d.get(mid, {})

        # Try to find provider from litellm model string
        provider = mid.split("/")[0] if "/" in mid else "unknown"

        # Find observed quota from availability.json
        obs_quota = None
        prov_data = avail.get("providers", {}).get(provider, {})
        model_data = prov_data.get(mid, {})
        rl_remaining = model_data.get("rl_remaining")
        rl_limit = model_data.get("rl_limit")
        rl_reset = model_data.get("rl_reset")

        model_rows.append({
            "model": mid,
            "provider": provider,
            "usage_24h": {
                "input_tokens": u24.get("input_tokens", 0),
                "output_tokens": u24.get("output_tokens", 0),
                "total_tokens": u24.get("total_tokens", 0),
                "num_requests": u24.get("num_requests", 0),
            },
            "usage_7d": {
                "input_tokens": u7.get("input_tokens", 0),
                "output_tokens": u7.get("output_tokens", 0),
                "total_tokens": u7.get("total_tokens", 0),
                "num_requests": u7.get("num_requests", 0),
            },
            "spend_7d": round(sp.get("spend", 0.0), 6),
            "observed_quota": {
                "remaining": rl_remaining,
                "limit": rl_limit,
                "reset_seconds": rl_reset,
            } if rl_remaining is not None or rl_limit is not None else None,
        })

    # Aggregate per provider
    by_provider = defaultdict(lambda: {
        "total_requests_24h": 0,
        "total_tokens_24h": 0,
        "total_requests_7d": 0,
        "total_tokens_7d": 0,
        "spend_7d": 0.0,
        "models": [],
    })
    for row in model_rows:
        p = row["provider"]
        by_provider[p]["total_requests_24h"] += row["usage_24h"]["num_requests"]
        by_provider[p]["total_tokens_24h"] += row["usage_24h"]["total_tokens"]
        by_provider[p]["total_requests_7d"] += row["usage_7d"]["num_requests"]
        by_provider[p]["total_tokens_7d"] += row["usage_7d"]["total_tokens"]
        by_provider[p]["spend_7d"] += row["spend_7d"]
        by_provider[p]["models"].append(row["model"])

    result = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "period_24h": {
            "total_requests": sum(r["usage_24h"]["num_requests"] for r in model_rows),
            "total_tokens": sum(r["usage_24h"]["total_tokens"] for r in model_rows),
        },
        "period_7d": {
            "total_requests": sum(r["usage_7d"]["num_requests"] for r in model_rows),
            "total_tokens": sum(r["usage_7d"]["total_tokens"] for r in model_rows),
        },
        "by_provider": dict(by_provider),
        "by_model": model_rows,
    }

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"[usage] Wrote {OUT_JSON.relative_to(ROOT)} — {len(model_rows)} models tracked")


if __name__ == "__main__":
    main()
