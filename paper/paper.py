#!/usr/bin/env python3
"""Paper-trading runner for the day-trader skill pack: a simulated $500 account.

Nothing here places a real order or talks to a broker. It runs the pack's own
tools (scanner -> exit -> budget -> risk) on live free Yahoo data, takes at most
one simulated trade a day by a fixed mechanical rule, and settles it after the
close from the day's real 5-minute bars.

    python3 paper/paper.py entry     # ~10:20 AM ET: scan, pick by rule, paper fill
    python3 paper/paper.py settle    # after 4:00 PM ET: stop / target / flat at 15:55
    python3 paper/paper.py report    # the daily returns table

The rule (fixed, so nobody is hand-picking):
  1. scan.py orb over the whole universe ($5+ stocks).
  2. Newest breakouts first (closest to the price now). Skip a chart whose live
     price is back at or under its opening-range high.
  3. Entry = the live price (that is the paper fill, plus $0.01 a share slippage),
     stop = opening-range midpoint, target = 2R, flat by 15:55.
  4. size.py sizes it to the account; check.py must PASS. The first that passes
     is the trade. None pass -> no trade today (a 0.00% day).
Settlement is conservative: a bar that touches both stop and target counts as
the stop; a gap past the stop fills at the bar's open; market exits pay $0.01.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SK = os.path.join(ROOT, ".claude", "skills")
TR = os.path.join(ROOT, "trader")
LEDGER = os.path.join(ROOT, "paper", "ledger.json")
START = 500.0
SLIP = 0.01
MAX_TRIES = 30

CONFIG = {
    "budget": START, "cash_buffer_pct": 10, "max_position_pct": 90,
    "max_order_usd": 450, "min_order_usd": 5,
    "max_loss_per_trade_usd": 10, "max_daily_loss_usd": 10,
    "max_trades_per_day": 1, "max_orders_per_day": 40, "min_reward_risk": 1.0,
    "require_stop": True, "no_entries_before": "09:45", "no_entries_after": "15:30",
    "flat_by": "15:55", "fractional": False, "broker": "alpaca", "paper": True,
    "blocked": [], "allow_options": False, "allow_margin": False,
    "allow_short": False, "pdt_guard": False,
}


def now_ny():
    return datetime.now(NY)


def load_ledger():
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            return json.load(f)
    return {"start": START, "equity": START, "open": None, "days": []}


def save_ledger(led):
    with open(LEDGER, "w") as f:
        json.dump(led, f, indent=2)
        f.write("\n")


def tool(script, *args):
    p = subprocess.run([sys.executable, os.path.join(SK, script), *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=900)
    return p.returncode, p.stdout + p.stderr


def chart(symbol):
    url = ("https://query2.finance.yahoo.com/v8/finance/chart/%s"
           "?range=1d&interval=5m" % symbol)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        res = json.load(r)["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        bars.append({"t": datetime.fromtimestamp(ts, NY), "o": o, "h": h, "l": l, "c": c})
    return res["meta"].get("regularMarketPrice"), bars


def entry():
    led = load_ledger()
    now = now_ny()
    today = now.strftime("%Y-%m-%d")
    if led["open"] or any(d["date"] == today for d in led["days"]):
        print("Already have a trade or a result for %s. Nothing to do." % today)
        return 0
    os.makedirs(TR, exist_ok=True)
    cfg = dict(CONFIG, budget=round(led["equity"], 2),
               max_order_usd=round(led["equity"] * 0.9, 2),
               max_loss_per_trade_usd=round(led["equity"] * 0.02, 2),
               max_daily_loss_usd=round(led["equity"] * 0.02, 2))
    with open(os.path.join(TR, "config.json"), "w") as f:
        json.dump(cfg, f, indent=2)
    code, out = tool("trader-scanner/scan.py", "orb", "--min-price", "5", "--quiet")
    print(out.strip().splitlines()[0] if out.strip() else "(no scanner output)")
    if code != 0 and any(w in out.lower() for w in ("closed", "holiday", "weekend")):
        print("Market closed today: nothing recorded.")
        return 0
    if code != 0:
        print("Scanner found nothing or could not run: no trade today.")
        led["days"].append({"date": today, "symbol": None, "pnl": 0.0,
                            "equity": round(led["equity"], 2), "note": out.strip()[-200:]})
        save_ledger(led)
        return 0
    with open(os.path.join(TR, "watchlist.json")) as f:
        hits = json.load(f)["hits"]
    hits.sort(key=lambda h: h.get("bar_index", 0), reverse=True)
    tried = []
    for h in hits[:MAX_TRIES]:
        sym = h["symbol"]
        try:
            live, _ = chart(sym)
        except Exception as e:  # noqa: BLE001
            tried.append("%s: no quote (%s)" % (sym, e.__class__.__name__))
            continue
        time.sleep(0.3)
        if not live or live <= h["or_high"]:
            tried.append("%s: back under its range high" % sym)
            continue
        fill = round(live + SLIP, 2)
        t = now_ny()
        c1, o1 = tool("trader-exit/exit.py", "plan", "--symbol", sym, "--entry", str(fill),
                      "--or-high", str(h["or_high"]), "--or-low", str(h["or_low"]),
                      "--date", today, "--time", t.strftime("%H:%M"), "--write")
        if c1 != 0:
            tried.append("%s: exit plan refused" % sym)
            continue
        op = os.path.join(TR, "orders.json")
        if os.path.exists(op):
            os.remove(op)
        c2, o2 = tool("trader-budget/size.py", "--write")
        if c2 != 0 or not os.path.exists(os.path.join(TR, "orders.json")):
            tried.append("%s: sizing refused (%s)" % (sym, o2.strip().splitlines()[-2][:90] if o2.strip() else "?"))
            continue
        c3, o3 = tool("trader-risk/check.py", "--json", "--no-write")
        try:
            r = json.loads(o3[o3.index("{"):])["results"][0]
        except Exception:  # noqa: BLE001
            tried.append("%s: risk check unreadable" % sym)
            continue
        if r["verdict"] != "PASS":
            tried.append("%s: risk BLOCK (%s)" % (sym, "; ".join(r["reasons"])[:90]))
            continue
        with open(os.path.join(TR, "orders.json")) as f:
            o = json.load(f)["orders"][0]
        qty = int(o["qty"])
        led["open"] = {"date": today, "symbol": sym, "qty": qty, "fill": fill,
                       "fill_time": t.strftime("%H:%M"),
                       "stop": float(o["stop_loss_stop_price"]),
                       "target": float(o["take_profit_limit_price"]),
                       "max_loss": r["max_loss"], "or_high": h["or_high"],
                       "or_low": h["or_low"], "skipped": tried}
        save_ledger(led)
        print("PAPER BUY %d %s @ %.2f at %s ET | stop %.2f | target %.2f | max loss $%.2f"
              % (qty, sym, fill, t.strftime("%H:%M"), led["open"]["stop"],
                 led["open"]["target"], r["max_loss"]))
        for line in tried:
            print("  skipped " + line)
        return 0
    for f in ("orders.json",):
        p = os.path.join(TR, f)
        if os.path.exists(p):
            os.remove(p)
    print("No breakout passed sizing and risk: no trade today.")
    for line in tried:
        print("  skipped " + line)
    led["days"].append({"date": today, "symbol": None, "pnl": 0.0,
                        "equity": round(led["equity"], 2), "note": "nothing passed"})
    save_ledger(led)
    return 0


def settle():
    led = load_ledger()
    pos = led["open"]
    if not pos:
        print("No open paper trade to settle.")
        return 0
    _, bars = chart(pos["symbol"])
    bars = [b for b in bars if b["t"].strftime("%Y-%m-%d") == pos["date"]]
    hh, mm = map(int, pos["fill_time"].split(":"))
    start_min = (hh * 60 + mm) // 5 * 5
    exit_px, how, when = None, None, None
    for b in bars:
        m = b["t"].hour * 60 + b["t"].minute
        if m < start_min:
            continue
        if m >= 15 * 60 + 55:
            break
        if b["o"] <= pos["stop"]:
            exit_px, how = b["o"] - SLIP, "gapped through the stop"
        elif b["l"] <= pos["stop"]:
            exit_px, how = pos["stop"] - SLIP, "stop"
        elif b["o"] >= pos["target"]:
            exit_px, how = b["o"], "gapped through the target"
        elif b["h"] >= pos["target"]:
            exit_px, how = pos["target"], "target"
        if exit_px is not None:
            when = b["t"].strftime("%H:%M")
            break
        if m == 15 * 60 + 50:
            exit_px, how, when = b["c"] - SLIP, "flat by 15:55", "15:55"
            break
    if exit_px is None:
        if not bars or now_ny().strftime("%H:%M") < "16:00":
            print("Session not finished (or no bars yet): try again after 4:00 PM ET.")
            return 1
        exit_px, how, when = bars[-1]["c"] - SLIP, "flat at the last bar", bars[-1]["t"].strftime("%H:%M")
    exit_px = round(exit_px, 2)
    pnl = round((exit_px - pos["fill"]) * pos["qty"], 2)
    before = led["equity"]
    led["equity"] = round(before + pnl, 2)
    day = dict(pos, exit=exit_px, exit_how=how, exit_time=when, pnl=pnl,
               ret_pct=round(pnl / before * 100, 2), equity=led["equity"])
    day.pop("skipped", None)
    led["days"].append(day)
    led["open"] = None
    save_ledger(led)
    print("SETTLED %s: %d @ %.2f -> %.2f (%s at %s) P&L $%+.2f (%+.2f%%), account $%.2f"
          % (pos["symbol"], pos["qty"], pos["fill"], exit_px, how, when, pnl,
             day["ret_pct"], led["equity"]))
    return 0


def report():
    led = load_ledger()
    print("Paper account, start $%.2f, now $%.2f (%+.2f%% total)"
          % (led["start"], led["equity"], (led["equity"] / led["start"] - 1) * 100))
    print("%-10s %-6s %5s %9s %9s  %-24s %9s %8s %10s" %
          ("date", "symbol", "qty", "fill", "exit", "how", "P&L", "day %", "account"))
    for d in led["days"]:
        if d.get("symbol"):
            print("%-10s %-6s %5d %9.2f %9.2f  %-24s %+9.2f %+7.2f%% %10.2f" %
                  (d["date"], d["symbol"], d["qty"], d["fill"], d["exit"],
                   d["exit_how"][:24], d["pnl"], d["ret_pct"], d["equity"]))
        else:
            print("%-10s %-6s %5s %9s %9s  %-24s %+9.2f %+7.2f%% %10.2f" %
                  (d["date"], "-", "-", "-", "-", "no trade", 0, 0, d["equity"]))
    if led["open"]:
        p = led["open"]
        print("OPEN: %d %s @ %.2f, stop %.2f, target %.2f" %
              (p["qty"], p["symbol"], p["fill"], p["stop"], p["target"]))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    sys.exit({"entry": entry, "settle": settle, "report": report}[cmd]())
