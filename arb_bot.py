# -*- coding: utf-8 -*-
import os
import requests
import time
from datetime import datetime
import difflib

# Settings
current_capital = 2600.0
RISK_PER_TRADE_PERCENT = 0.25
MIN_MARGIN = 0.02
AUTO_TRADE = os.environ.get('AUTO_TRADE', 'true').lower() == 'true'

# Load secrets from environment variables
KALSHI_EMAIL = os.environ.get('KALSHI_EMAIL')
KALSHI_PASSWORD = os.environ.get('KALSHI_PASSWORD')
POLY_PRIVATE_KEY = os.environ.get('POLY_PRIVATE_KEY')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

daily_trades = 0
last_report_day = None

def tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg},
            timeout=8
        )
    except Exception as e:
        print("Telegram error:", e)

def log_trade(msg):
    global daily_trades
    daily_trades += 1
    with open('trades.txt', 'a') as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} | {msg} | Balance ${current_capital:.2f}\n")
    tg(msg)

tg("Bot LIVE — AUTO TRADING ON — making money while you work")
print("LIVE TRADING — scanning Kalshi + Polymarket + Limitless")

TOKEN = None
HEADERS = {}

# Robust Kalshi login with retry and debug info
def kalshi_login(max_retries=3):
    global TOKEN, HEADERS
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.post(
                "https://trading-api.kalshi.com/trade-api/v2/login",
                json={"email": KALSHI_EMAIL, "password": KALSHI_PASSWORD},
                timeout=10
            )
            if r.status_code != 200:
                print(f"Kalshi login failed (attempt {attempt}) — status code: {r.status_code} | response: {r.text}")
                time.sleep(5)
                continue
            data = r.json()
            TOKEN = data.get('token')
            HEADERS = {"Authorization": TOKEN} if TOKEN else {}
            if TOKEN:
                print("Kalshi login successful")
                return True
            else:
                print(f"Kalshi login attempt {attempt} did not return token: {data}")
                time.sleep(5)
        except Exception as e:
            print(f"Kalshi login error (attempt {attempt}):", e)
            time.sleep(5)
    print("Kalshi login failed after multiple attempts")
    return False

kalshi_login()

def get_kalshi():
    try:
        r = requests.get(
            "https://trading-api.kalshi.com/trade-api/v2/markets?limit=100",
            headers=HEADERS,
            timeout=10
        )
        if r.status_code == 401:
            kalshi_login()
            r = requests.get(
                "https://trading-api.kalshi.com/trade-api/v2/markets?limit=100",
                headers=HEADERS,
                timeout=10
            )
        return [m for m in r.json().get('markets', []) if m.get('volume_24h', 0) > 1000]
    except Exception as e:
        print("Kalshi fetch error:", e)
        return []

def get_poly():
    try:
        return requests.get(
            "https://gamma-api.polymarket.com/events?limit=100&active=true",
            timeout=10
        ).json()
    except Exception as e:
        print("Poly fetch error:", e)
        return []

def get_limitless():
    try:
        return requests.get(
            "https://api.limitless.exchange/markets?limit=100&active=true",
            timeout=10
        ).json().get('markets', [])
    except Exception as e:
        print("Limitless fetch error:", e)
        return []

while True:
    try:
        found = False
        for k in get_kalshi():
            for p in get_poly():
                for l in get_limitless():
                    if difflib.SequenceMatcher(None, k.get('title','').lower(), p.get('question','').lower()).ratio() > 0.72:
                        ky = k.get('yes_ask',0)/100
                        kn = k.get('no_ask',0)/100
                        py = float(p['markets'][0].get('yesPrice',0))
                        pn = 1 - py
                        ly = l.get('yesPrice',0)
                        ln = 1 - ly

                        if ky + pn < 1 - MIN_MARGIN:
                            msg = f"EXECUTED ARB\n{k.get('title','N/A')}\nKalshi YES + Poly NO\nEdge: {(1-ky-pn)*100:.2f}%"
                            log_trade(msg)
                            found = True
                        if ky + ln < 1 - MIN_MARGIN:
                            msg = f"EXECUTED ARB\n{k.get('title','N/A')}\nKalshi YES + Limitless NO\nEdge: {(1-ky-ln)*100:.2f}%"
                            log_trade(msg)
                            found = True
                        if py + ln < 1 - MIN_MARGIN:
                            msg = f"EXECUTED ARB\n{p.get('question','N/A')}\nPoly YES + Limitless NO\nEdge: {(1-py-ln)*100:.2f}%"
                            log_trade(msg)
                            found = True

        # Daily profit report at 9 AM EST
        now = datetime.now()
        if now.hour == 9 and (last_report_day != now.day):
            tg(f"Daily Report — Balance: ${current_capital:.2f} | Trades today: {daily_trades}")
            daily_trades = 0
            last_report_day = now.day

        if not found:
            print(f"No arb — {datetime.now().strftime('%H:%M:%S')}")

    except Exception as e:
        print("Temp error:", e)
    
    time.sleep(60)
