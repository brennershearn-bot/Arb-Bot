# -*- coding: utf-8 -*-
# LIVE Kalshi + Polymarket Arb Bot
import os
import asyncio
import aiohttp
import time
import json
from decimal import Decimal, getcontext, ROUND_HALF_UP
from datetime import datetime
from difflib import SequenceMatcher
from web3 import Web3
from web3.middleware import geth_poa_middleware
from eth_account import Account

# ---------- CONFIG ----------
getcontext().prec = 9
STARTING_CAPITAL = Decimal(os.environ.get("STARTING_CAPITAL", "200"))
RISK_PER_TRADE_PERCENT = Decimal(os.environ.get("RISK_PER_TRADE_PERCENT", "0.25"))
MIN_EDGE = Decimal("0.03")
MAX_PER_MARKET = Decimal("0.25")
MAX_PER_TRADE_CAP = Decimal("0.5")
AUTO_TRADE = True
DRY_RUN = False
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

KALSHI_EMAIL = os.environ.get("KALSHI_EMAIL")
KALSHI_PASSWORD = os.environ.get("KALSHI_PASSWORD")
POLY_PRIVATE_KEY = os.environ.get("POLY_PRIVATE_KEY")
POLY_PUBLIC_ADDRESS = os.environ.get("POLY_PUBLIC_ADDRESS")
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
    base = capital_now * RISK_PER_TRADE_PERCENT
    scale = Decimal("1") + (edge / (MIN_EDGE if MIN_EDGE > 0 else Decimal("0.01")))
    stake = base * scale
    stake_cap = capital_now * MAX_PER_MARKET
    max_trade_cap = capital_now * MAX_PER_TRADE_CAP
    stake = min(stake, stake_cap, max_trade_cap)
    return quantize_d(stake)

# ---------- WEB3 SETUP ----------
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)
account = Account.from_key(POLY_PRIVATE_KEY)

# Example Polymarket Market ABI (simplified, standard for "buy" function)
POLYMARKET_ABI = [
    {
        "inputs":[
            {"internalType":"uint256","name":"_outcomeIndex","type":"uint256"},
            {"internalType":"uint256","name":"_amount","type":"uint256"},
            {"internalType":"uint256","name":"_price","type":"uint256"}
        ],
        "name":"buy",
        "outputs":[],
        "stateMutability":"payable",
        "type":"function"
    }
]

# ---------- KALSHI ----------
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
kalshi_token = None

async def kalshi_login(session):
    global kalshi_token
    payload = {"email": KALSHI_EMAIL, "password": KALSHI_PASSWORD}
    async with session.post(f"{KALSHI_BASE}/sessions", json=payload, timeout=10) as r:
        data = await r.json()
        kalshi_token = data.get("token")
        await tg("Kalshi login successful.")

async def fetch_kalshi(session):
    if not kalshi_token:
        await kalshi_login(session)
    headers = {"Authorization": f"Bearer {kalshi_token}"}
    async with session.get(f"{KALSHI_BASE}/markets?limit=200", headers=headers, timeout=10) as r:
        if r.status == 401:
            return []
        data = await r.json()
        markets = data.get("markets", [])
        return [m for m in markets if (m.get("volume_24h") or 0) > 1000]

async def place_order_kalshi(session, market_id, side, price, stake):
    headers = {"Authorization": f"Bearer {kalshi_token}"}
    payload = {"marketId": market_id, "side": side, "price": float(price), "quantity": float(stake)}
    async with session.post(f"{KALSHI_BASE}/orders", headers=headers, json=payload, timeout=10) as r:
        data = await r.json()
        success = r.status == 200
        return {"success": success, "filled": stake if success else Decimal("0"), "order_id": data.get("id")}

# ---------- POLYMARKET ----------
POLY_BASE = "https://gamma-api.polymarket.com"

async def fetch_poly(session):
    async with session.get(f"{POLY_BASE}/events?limit=200&active=true", timeout=10) as r:
        return await r.json()

async def place_order_polymarket(session, event_id, side, price, stake):
    # Determine outcome index: 0 = NO, 1 = YES
    outcome_index = 1 if side.lower() == "buy" else 0
    contract_address = Web3.to_checksum_address(event_id)  # event_id = market contract
    contract = w3.eth.contract(address=contract_address, abi=POLYMARKET_ABI)
    # Amount in smallest unit (wei)
    amount_wei = w3.to_wei(float(stake), 'ether')
    price_scaled = int(price * 1e18)
    txn = contract.functions.buy(outcome_index, amount_wei, price_scaled).build_transaction({
        'from': POLY_PUBLIC_ADDRESS,
        'value': amount_wei,
        'gas': 300000,
        'gasPrice': w3.to_wei('50', 'gwei'),
        'nonce': w3.eth.get_transaction_count(POLY_PUBLIC_ADDRESS)
    })
    signed_txn = w3.eth.account.sign_transaction(txn, private_key=POLY_PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    return {"success": receipt.status == 1, "filled": stake, "order_id": tx_hash.hex()}

# ---------- ARBITRAGE ----------
async def try_execute_arb(session, kalshi_m, poly_ev):
    global capital, daily_trades, open_exposure
    try:
        ky = Decimal(str(kalshi_m.get("yes_ask") or kalshi_m.get("yes_bid") or 0)) / Decimal("100")
        py_raw = (poly_ev.get("markets")[0].get("yesPrice", 0)) if poly_ev.get("markets") else 0
        py = Decimal(str(py_raw))
        pn = Decimal("1") - py
        raw_edge = Decimal("1") - ky - pn
        edge_net = raw_edge - (FEE_EST * 2) - (SLIPPAGE_EST * 2)
        if edge_net < MIN_EDGE:
            return False
        stake = compute_dynamic_stake(edge_net, capital)
        if (open_exposure + stake) > (capital * MAX_TOTAL_EXPOSURE_FRAC):
            return False
        # Execute
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
            rec = {"timestamp": now_ts(), "combo":"kalshi_poly","edge_net":str(edge_net),"stake":str(stake),"pnl":str(approx_profit),"legs":leg_fills}
            log_trade_record(rec)
            await tg(f"ARB EXECUTED stake ${stake} edge {edge_net:.4f} pnl {approx_profit:.2f}")
            return True
        return False
    except Exception as e:
        await tg(f"ARB ERROR: {e}")
        return False

# ---------- MAIN LOOP ----------
async def main():
    global daily_trades, last_report_day, open_exposure
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                k_task = asyncio.create_task(fetch_kalshi(session))
                p_task = asyncio.create_task(fetch_poly(session))
                kalshi_markets, poly_response = await asyncio.gather(k_task, p_task)
                poly_list = poly_response.get("events") if isinstance(poly_response, dict) else []
                for k_m in kalshi_markets:
                    for p_ev in poly_list:
                        if fuzzy_ratio(k_m.get("title","").lower(), p_ev.get("question","").lower()) < 0.72:
                            continue
                        await try_execute_arb(session, k_m, p_ev)
                now = datetime.utcnow()
                if now.hour == 9 and (last_report_day != now.day):
                    await tg(f"Daily Report — Balance: ${capital:.2f} | Trades today: {daily_trades}")
                    daily_trades = 0
                    last_report_day = now.day
            except Exception as e:
                await tg(f"Main loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    print("Starting LIVE Kalshi + Polymarket Arb Bot")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down cleanly.")
