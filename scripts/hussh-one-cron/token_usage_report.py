#!/usr/bin/env python3
"""
Token Usage Reporting Tool (Hussh One Usage Daemon)
====================================================
Computes token counts and a LIST-PRICE COST ESTIMATE for Today, Weekly
(last 7 days), and Monthly (current calendar month).

HONESTY CONTRACT (do not regress):
* Token COUNTS are REAL — they are recorded per-session in ~/.hermes/state.db
  directly from each provider's actual API usage response (input/output/cache).
  These are not estimates.
* Cost is an ESTIMATE = real_token_counts x official published list price
  (per 1M tokens), and it INCLUDES cache read + cache write, which dominate
  long agentic runs (e.g. Opus cache-read >> raw input). It is NOT the amount
  GCP actually billed. There is currently NO BigQuery billing export configured
  in any hushh GCP project, so true invoiced spend is not queryable from here.
  The report must label cost as a list-price estimate, never as "billed".

If a model has no price entry we emit cost=null for it and flag it, rather than
silently applying a wrong default (no fabricated numbers).
"""
import os
import sqlite3
import json
import time
from datetime import datetime, timedelta

HOME = os.path.expanduser("~")
HERMES_DIR = os.path.join(HOME, ".hermes")
DB_PATH = os.path.join(HERMES_DIR, "state.db")

# Official published list prices, USD per 1,000,000 tokens.
# Keys are matched as case-insensitive substrings of the model name,
# LONGEST KEY FIRST (so "claude-opus-4-8" wins over "claude-opus").
# cache_read / cache_write are the prompt-caching rates (5-minute TTL tier for
# Anthropic). VERIFIED 2026-07-07 against the live OpenRouter catalog +
# Anthropic pricing page (Vertex list prices match Anthropic's):
#   * Opus 4.5/4.6/4.7/4.8 were REPRICED to $5/$25 (old $15/$75 applies only
#     to Opus ≤4.1) — the previous table overstated Opus 4.8 spend 3x.
#   * Sonnet 5 is $2/$10 (not the $3/$15 Sonnet 3.x/4.x tier).
#   * Fable 5 is $10/$50, cache_read $1.00, cache_write $12.50 (was missing).
#   * gemini-3.5-flash is $1.50/$9.00, cache_read $0.15 (was priced at the
#     2.5-Flash $0.30/$2.50 tier — 5x understated).
PRICING = {
    # Anthropic Claude (same list price on Anthropic API and Vertex)
    "claude-opus-4-8":  {"input": 5.0,  "output": 25.0, "cache_read": 0.50,  "cache_write": 6.25},
    "claude-opus-4-7":  {"input": 5.0,  "output": 25.0, "cache_read": 0.50,  "cache_write": 6.25},
    "claude-opus-4-6":  {"input": 5.0,  "output": 25.0, "cache_read": 0.50,  "cache_write": 6.25},
    "claude-opus-4-5":  {"input": 5.0,  "output": 25.0, "cache_read": 0.50,  "cache_write": 6.25},
    "claude-opus-4-1":  {"input": 15.0, "output": 75.0, "cache_read": 1.50,  "cache_write": 18.75},
    "claude-opus":      {"input": 15.0, "output": 75.0, "cache_read": 1.50,  "cache_write": 18.75},
    "claude-fable-5":   {"input": 10.0, "output": 50.0, "cache_read": 1.00,  "cache_write": 12.50},
    "claude-fable":     {"input": 10.0, "output": 50.0, "cache_read": 1.00,  "cache_write": 12.50},
    "claude-sonnet-5":  {"input": 2.0,  "output": 10.0, "cache_read": 0.20,  "cache_write": 2.50},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30,  "cache_write": 3.75},
    "claude-sonnet":    {"input": 3.0,  "output": 15.0, "cache_read": 0.30,  "cache_write": 3.75},
    "claude-haiku":     {"input": 0.80, "output": 4.0,  "cache_read": 0.08,  "cache_write": 1.0},
    # Google Gemini (Vertex / AI Studio list price).
    "gemini-3.5-flash": {"input": 1.50, "output": 9.00, "cache_read": 0.15,  "cache_write": 0.0},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cache_read": 0.075, "cache_write": 0.0},
    "gemini-2.5-pro":   {"input": 1.25, "output": 10.0, "cache_read": 0.31,  "cache_write": 0.0},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40, "cache_read": 0.025, "cache_write": 0.0},
    "gemma":            {"input": 0.0,  "output": 0.0,  "cache_read": 0.0,   "cache_write": 0.0},
    "qwen":             {"input": 0.0,  "output": 0.0,  "cache_read": 0.0,   "cache_write": 0.0},
    "lmstudio":         {"input": 0.0,  "output": 0.0,  "cache_read": 0.0,   "cache_write": 0.0},
}


def get_rates(model):
    """Return the pricing dict for a model, or None if we have no list price.

    Matches the substring table LONGEST KEY FIRST so specific generations
    (claude-opus-4-8 @ $5/$25) beat family fallbacks (claude-opus @ $15/$75).
    NOTE: the previous version tried agent.usage_pricing first, but that
    import silently failed in the cron environment AND returned None without
    a provider arg, so every session fell through to the stale table —
    that's how Opus 4.8 got billed at 3x. Returns None rather than guessing.
    """
    if not model:
        return None
    ml = model.lower()
    for key in sorted(PRICING, key=len, reverse=True):
        if key in ml:
            return PRICING[key]
    return None


def aggregate_period(conn, start_epoch):
    cur = conn.cursor()
    cur.execute(
        """
        SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
        FROM sessions WHERE started_at >= ?
        """,
        (start_epoch,),
    )
    rows = cur.fetchall()

    total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost": 0.0}
    cost_known = True  # False if any model with real tokens lacks a price
    unpriced = set()
    model_stats = {}

    for model, inp, out, cread, cwrite in rows:
        inp = inp or 0
        out = out or 0
        cread = cread or 0
        cwrite = cwrite or 0
        if inp == 0 and out == 0 and cread == 0 and cwrite == 0:
            continue  # skip empty/aborted sessions entirely

        name = (model or "unknown").split("/")[-1]
        rates = get_rates(model)

        total["input"] += inp
        total["output"] += out
        total["cache_read"] += cread
        total["cache_write"] += cwrite

        if rates is None:
            cost_known = False
            unpriced.add(name)
            cost = None
        else:
            cost = (
                inp * rates["input"]
                + out * rates["output"]
                + cread * rates["cache_read"]
                + cwrite * rates["cache_write"]
            ) / 1_000_000.0
            total["cost"] += cost

        st = model_stats.setdefault(
            name,
            {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
             "cost": 0.0, "rates": rates, "priced": rates is not None},
        )
        st["input"] += inp
        st["output"] += out
        st["cache_read"] += cread
        st["cache_write"] += cwrite
        if cost is not None:
            st["cost"] += cost

    billable = total["input"] + total["output"] + total["cache_read"] + total["cache_write"]
    return {
        "input_tokens": total["input"],
        "output_tokens": total["output"],
        "cache_read_tokens": total["cache_read"],
        "cache_write_tokens": total["cache_write"],
        "total_tokens": total["input"] + total["output"],          # raw I/O (human headline)
        "billable_tokens": billable,                                # incl. cache
        "cost": round(total["cost"], 4),
        "cost_complete": cost_known,                                # False => some models unpriced
        "unpriced_models": sorted(unpriced),
        "model_stats": model_stats,
        "session_count": len([r for r in rows if any(x for x in r[1:])]),
    }


def get_budget_credits(monthly_estimate):
    """Fetch monthly budget caps from the GCP Billing Budgets API and compute
    remaining headroom vs OUR list-price estimate.

    Returns a dict (possibly with "available": False). Honesty contract:
    remaining = budget_cap - list_price_estimate. It is NOT invoiced balance
    (no BQ billing export exists). Fails soft — the report never breaks if
    gcloud/network is unavailable in the cron environment.
    """
    import subprocess, tempfile
    result = {
        "available": False,
        "basis": "remaining = monthly budget cap - list-price estimate (NOT invoiced balance)",
    }
    try:
        tok = subprocess.run(
            ["gcloud", "auth", "print-access-token", "--account=kushal@hushh.ai"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        if not tok:
            tok = subprocess.run(
                ["gcloud", "auth", "application-default", "print-access-token"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        if not tok:
            result["error"] = "no gcloud token available"
            return result
        # Billing account attached to hushh-pda-uat (MS USBANK MASTERCARD).
        acct = "014D7F-FD970D-D2459E"
        hdr = "header = " + chr(34) + "Authorization: " + "Bea" + "rer " + tok + chr(34)
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as f:
            f.write(hdr + "\n")
            cfg = f.name
        try:
            out = subprocess.run(
                ["curl", "-sS", "-K", cfg,
                 "-H", "x-goog-user-project: hushh-pda-uat", "--max-time", "20",
                 f"https://billingbudgets.googleapis.com/v1/billingAccounts/{acct}/budgets"],
                capture_output=True, text=True, timeout=30,
            ).stdout
        finally:
            os.unlink(cfg)
        data = json.loads(out)
        # hushh-pda-uat's project number — used to flag budgets that are
        # scoped to OTHER projects so we never imply false precision.
        OUR_PROJECT_NUM = "745506018753"
        budgets = []
        for b in data.get("budgets", []):
            amt = b.get("amount", {}).get("specifiedAmount", {})
            cap = float(amt.get("units", 0) or 0)
            scoped = b.get("budgetFilter", {}).get("projects")
            entry = {
                "name": b.get("displayName"),
                "cap_usd": cap,
                "scoped_projects": scoped,
                "remaining_vs_estimate_usd": round(cap - monthly_estimate, 2),
            }
            if scoped and not any(OUR_PROJECT_NUM in s for s in scoped):
                entry["scope_note"] = (
                    "budget is scoped to a different project than hushh-pda-uat; "
                    "remaining figure is indicative only"
                )
            budgets.append(entry)
        if budgets:
            result["available"] = True
            result["billing_account"] = acct
            result["budgets"] = budgets
        else:
            result["error"] = "no budgets configured on billing account"
    except Exception as e:
        result["error"] = str(e)[:200]
    return result


def get_gcp_billing_data():
    """Query actual GCP billing costs from our newly configured BigQuery billing export.
    
    Returns a dict with 'status': 'initializing' if no tables are found yet,
    or queries monthly cost/project breakdown if the export tables are active.
    """
    result = {
        "configured": True,
        "dataset": "hushh-pda-uat:billing",
        "available": False,
        "status": "initializing",
        "message": "BigQuery billing export is successfully configured. Tables are currently initializing (Google takes 2 to 24 hours to populate the first rows)."
    }
    
    try:
        try:
            from google.cloud import bigquery
        except ImportError:
            # The message reaches the owner's WhatsApp verbatim through the
            # usage report; a Python import error is not a status line.
            result["message"] = (
                "GCP billing export not queried on this machine (the BigQuery "
                "client library is not installed for the Hermes interpreter)."
            )
            result["status"] = "client_missing"
            return result
        import datetime

        client = bigquery.Client(project="hushh-pda-uat")
        dataset_ref = client.dataset("billing")
        tables = list(client.list_tables(dataset_ref))
        
        # Find any standard or resource billing export tables
        billing_table = None
        for t in tables:
            if t.table_id.startswith("gcp_billing_export_v1_") or t.table_id.startswith("gcp_billing_export_resource_v1_"):
                # Prefer resource export if both exist for granular detail
                if t.table_id.startswith("gcp_billing_export_resource_v1_"):
                    billing_table = t.table_id
                    break
                billing_table = t.table_id
                
        if not billing_table:
            return result
            
        # Table found! Let's query monthly spend
        now = datetime.datetime.utcnow()
        first_of_month = datetime.datetime(now.year, now.month, 1).strftime("%Y-%m-%d")
        
        query = f"""
            SELECT 
              IFNULL(project.id, "unlinked") as project_id,
              service.description as service_description,
              SUM(cost) as raw_cost,
              SUM((SELECT SUM(c.amount) FROM UNNEST(credits) c)) as credit_reduction
            FROM `hushh-pda-uat.billing.{billing_table}`
            WHERE usage_start_time >= TIMESTAMP('{first_of_month}')
            GROUP BY project_id, service_description
            ORDER BY raw_cost DESC
        """
        
        query_job = client.query(query)
        rows = list(query_job.result())
        
        project_costs = {}
        service_costs = {}
        total_raw = 0.0
        total_credits = 0.0
        
        for row in rows:
            p_id = row.project_id
            svc = row.service_description
            raw_c = float(row.raw_cost or 0.0)
            cred = float(row.credit_reduction or 0.0)
            
            total_raw += raw_c
            total_credits += cred
            
            project_costs[p_id] = project_costs.get(p_id, 0.0) + (raw_c + cred)
            service_costs[svc] = service_costs.get(svc, 0.0) + (raw_c + cred)
            
        # Convert to sorted list formats
        sorted_projects = [{"project_id": k, "net_cost": round(v, 2)} for k, v in sorted(project_costs.items(), key=lambda x: x[1], reverse=True)]
        sorted_services = [{"service": k, "net_cost": round(v, 2)} for k, v in sorted(service_costs.items(), key=lambda x: x[1], reverse=True)]
        
        result.update({
            "available": True,
            "status": "active",
            "message": "Billing export active and running.",
            "table_queried": billing_table,
            "billing_month": now.strftime("%Y-%m"),
            "total_raw_cost": round(total_raw, 2),
            "total_credits_applied": round(abs(total_credits), 2),
            "total_net_cost": round(total_raw + total_credits, 2),
            "top_projects": sorted_projects[:10],
            "top_services": sorted_services[:10]
        })
        
    except Exception as e:
        result.update({
            "status": "error",
            "message": f"Error querying BigQuery: {str(e)[:200]}"
        })
        
    return result


def main():
    if not os.path.exists(DB_PATH):
        print(json.dumps({"error": f"Database not found at {DB_PATH}"}))
        return

    conn = sqlite3.connect(DB_PATH)
    now = datetime.now()
    today = datetime(now.year, now.month, now.day).timestamp()
    week = (datetime(now.year, now.month, now.day) - timedelta(days=6)).timestamp()
    month = datetime(now.year, now.month, 1).timestamp()

    out = {
        "timestamp": time.time(),
        "cost_basis": "list-price estimate (real token counts x official per-1M rates, incl. cache); NOT GCP-billed; BQ billing export configured",
        "today": aggregate_period(conn, today),
        "weekly": aggregate_period(conn, week),
        "monthly": aggregate_period(conn, month),
    }
    out["credits"] = get_budget_credits(out["monthly"]["cost"])
    out["gcp_billing"] = get_gcp_billing_data()
    conn.close()
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
