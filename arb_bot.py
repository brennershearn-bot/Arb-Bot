# -*- coding: utf-8 -*-
# Kalshi + Polymarket Arbitrage Bot
# Fully async, dynamic sizing, Telegram notifications
# No Limitless exchange

import os
import asyncio
import aiohttp
import time
import json
from decimal import Decimal, getcontext, ROUND_HALF_UP
from datetime import datetime
from difflib import SequenceMatcher

# ---------- CONFIG ----------
getcontext().prec = 9

STARTING_CAPITAL = Decimal(os.environ.get("STARTING_CAPITAL", "2600"))
RISK_PER_TRADE_PERCENT = Decimal(os.environ.get("RISK_PER_TRADE_PERCENT", "0.25"))
MIN_EDGE = Decimal(os.environ.get("MIN_EDGE", "0.03"))  # 3% default from backtest
MAX_PER_MARKET = Decimal(os.environ.get("MAX_PER_MARKET", "0.25"))
MAX_PER_TRADE_CAP = Decimal(os.environ.get("MAX_PER_TRADE_CAP", "0.5"))
AUTO_TRADE = os.environ.get("AUTO_TRADE", "true").lower() == "true"
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

KALSHI_EMAIL = os.environ.get("KALSHI_EMAIL")
KALSHI_PASSWORD = os.environ.get("KALSHI_PASSWORD")
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SLIPPAGE_EST = Decimal("0.6") / Decimal("100")
FEE_EST = Decimal("0.4") / Decimal("100")
MAX_DAILY_TRADES = int(os.environ.get("MAX_DAILY_TRADES", "500"))
MAX_TOTAL_EXPOSURE_FRAC = Decimal("0.9")

# ---------- GLOBAL STATE ----------
capital = STARTING_CAPITAL
daily_trades = 0
last_report_day = None
open_exposure = Decimal("0")

# ---------- UTIL ----------
def now_ts():
    return datetime.utcnow().isoformat()

def quantize_d(x: Decimal, q="0.01"):
    return x.quantize(Decimal(q), rounding=ROUND_HALF_UP)

def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()

# Telegram
async def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"TG-OFF: {msg}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, data=payload, timeout=5)
    except Exception:
        print("Telegram failed (ignored)")

def log_trade_record(rec):
    with open("trades.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("TRADE:", rec)

def compute_dynamic_stake(edge: Decimal, capital_now: Decimal) -> Decimal:
    base = capital_now * (RISK_PER_TRADE_PERCENT)
    scale = (Decimal("1") + (edge / (MIN_EDGE if MIN_EDGE > 0 else Decimal("0.01"))))
    stake = base * scale
    stake_cap = capital_now * MAX_PER_MARKET
    max_trade_cap = capital_now * MAX_PER_TRADE_CAP
    stake = min(stake, stake_cap, max_trade_cap)
    return quantize_d(stake)

# ---------- FETCHERS ----------
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLY_BASE = "https://gamma-api.polymarket.com"

async def fetch_kalshi(session):
    try:
        async with session.get(f"{KALSHI_BASE}/markets?limit=200", timeout=10) as r:
            if r.status == 401:
                return []
            data = await r.json()
            markets = data.get("markets", [])
            return [m for m in markets if (m.get("volume_24h") or 0) > 1000]
    except Exception as e:
        print("Kalshi fetch error:", e)
        return []

async def fetch_poly(session):
    try:
        async with session.get(f"{POLY_BASE}/events?limit=200&active=true", timeout=10) as r:
            data = await r.json()
            return data
    except Exception as e:
        print("Poly fetch error:", e)
        return []

# ---------- PLACEHOLDER ORDER FUNCTIONS ----------
async def place_order_kalshi(session, market_id, side, price, stake):
    if DRY_RUN or not AUTO_TRADE:
        import random
        if random.random() < 0.887:
            filled = stake
            return {"success": True, "filled": quantize_d(filled), "avg_fill_price": quantize_d(price), "order_id": "SIM_KALSHI"}
        else:
            return {"success": False, "filled": Decimal("0"), "order_id": "SIM_KALSHI_PARTIAL"}
    raise NotImplementedError("Replace with real Kalshi order call")

async def place_order_polymarket(session, event_id, side, price, stake):
    if DRY_RUN or not AUTO_TRADE:
        import random
        if random.random() < 0.887:
            filled = stake
            return {"success": True, "filled": quantize_d(filled), "avg_fill_price": quantize_d(price), "order_id": "SIM_POLY"}
        else:
            return {"success": False, "filled": Decimal("0"), "order_id": "SIM_POLY_PARTIAL"}
    raise NotImplementedError("Replace with real Polymarket order call")

# ---------- EXECUTION ----------
async def try_execute_arb(session, kalshi_m, poly_ev):
    global capital, daily_trades, open_exposure
    try:
        ky = Decimal(str(kalshi_m.get("yes_ask", 0))) / Decimal("100")
        py = Decimal(str((poly_ev.get("markets")[0].get("yesPrice", 0)))) if poly_ev.get("markets") else None
    except Exception:
        return False

    if py is None or ky is None:
        return False

    pn = Decimal("1") - py
    raw_edge = Decimal("1") - ky - pn
    edge_net = raw_edge - (FEE_EST * 2) - (SLIPPAGE_EST * 2)
    if edge_net < MIN_EDGE:
        return False

    stake = compute_dynamic_stake(edge_net, capital)
    if (open_exposure + stake) > (capital * MAX_TOTAL_EXPOSURE_FRAC):
        return False

    # Execute both legs
    tasks = [
        place_order_kalshi(session, kalshi_m.get("id"), "buy", ky, stake),
        place_order_polymarket(session, poly_ev.get("id"), "sell", pn, stake)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    leg_fills = []
    for r in results:
        if isinstance(r, Exception):
            leg_fills.append({"success": False, "filled": Decimal("0"), "err": str(r)})
        else:
            leg_fills.append(r)

    both_filled = all((r.get("filled") or Decimal("0")) >= stake * Decimal("0.95") for r in leg_fills)
    if both_filled:
        approx_profit = quantize_d(edge_net * stake)
        capital += approx_profit
        open_exposure -= stake
        daily_trades += 1
        rec = {
            "timestamp": now_ts(),
            "combo": "kalshi_poly",
            "edge_net": str(edge_net),
            "stake": str(stake),
            "pnl": str(approx_profit),
            "legs": leg_fills
        }
        log_trade_record(rec)
        await tg(f"ARB EXECUTED stake ${stake} edge {edge_net:.4f} pnl {approx_profit:.2f}")
        return True
    return False

# ---------- MAIN LOOP ----------
async def main():
    global daily_trades, last_report_day, open_exposure
    async with aiohttp.ClientSession() as session:
        while True:
            cycle_start = time.time()
            try:
                k_task = asyncio.create_task(fetch_kalshi(session))
                p_task = asyncio.create_task(fetch_poly(session))
                kalshi_markets, poly_response = await asyncio.gather(k_task, p_task)
                poly_list = poly_response.get("events") if isinstance(poly_response, dict) else []
                found_any = False
                for k_m in kalshi_markets:
                    for p_ev in poly_list:
                        if fuzzy_ratio(k_m.get("title","").lower(), p_ev.get("question","").lower()) < 0.72:
                            continue
                        success = await try_execute_arb(session, k_m, p_ev)
                        if success:
                            found_any = True
                            if daily_trades >= MAX_DAILY_TRADES:
                                break
                    if daily_trades >= MAX_DAILY_TRADES:
                        break
                now = datetime.utcnow()
                if now.hour == 9 and (last_report_day != now.day):
                    await tg(f"Daily Report — Balance: ${capital:.2f} | Trades today: {daily_trades}")
                    daily_trades = 0
                    last_report_day = now.day
            except Exception as e:
                await tg(f"Main loop error: {e}")
            elapsed = time.time() - cycle_start
            await asyncio.sleep(max(0, POLL_INTERVAL - elapsed))

if __name__ == "__main__":
    print("Starting Kalshi + Polymarket Arb Bot — DRY_RUN:", DRY_RUN, "AUTO_TRADE:", AUTO_TRADE)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down cleanly.")
