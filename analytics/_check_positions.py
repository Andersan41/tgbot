import ccxt
import os
from dotenv import load_dotenv
load_dotenv()

exchange = ccxt.bingx({
    'apiKey': os.getenv('EXCHANGE_API_KEY', ''),
    'secret': os.getenv('EXCHANGE_API_SECRET', ''),
    'options': {'defaultType': 'swap'},
})
exchange.load_markets()

positions = [
    {'sym': 'APE/USDT:USDT', 'side': 'SELL', 'entry': 0.1478, 'sl': 0.15196, 'tp': 0.1415, 'created': '2026-07-17'},
    {'sym': 'ZEC/USDT:USDT', 'side': 'SELL', 'entry': 538.57, 'sl': 562.80, 'tp': 461.07, 'created': '2026-07-17'},
    {'sym': 'ATOM/USDT:USDT', 'side': 'SELL', 'entry': 1.507, 'sl': 1.5388, 'tp': 1.4582, 'created': '2026-07-17'},
]

print(f"{'Symbol':<15s} {'Entry':>10s} {'SL':>10s} {'TP':>10s} {'Current':>10s} {'PnL%':>8s} {'Status'}")
print("-" * 80)

for p in positions:
    try:
        ticker = exchange.fetch_ticker(p['sym'])
        price = ticker['last']
        entry = p['entry']
        sl = p['sl']
        tp = p['tp']
        pnl_pct = (entry - price) / entry * 100 if p['side'] == 'SELL' else (price - entry) / entry * 100

        if p['side'] == 'SELL':
            if price >= sl:
                status = "SL HIT"
            elif price <= tp:
                status = "TP HIT"
            else:
                status = "OPEN"
        else:
            if price <= sl:
                status = "SL HIT"
            elif price >= tp:
                status = "TP HIT"
            else:
                status = "OPEN"

        print(f"{p['sym']:<15s} {entry:>10.4f} {sl:>10.4f} {tp:>10.4f} {price:>10.4f} {pnl_pct:>+7.2f}% {status}")
    except Exception as e:
        print(f"{p['sym']:<15s} ERROR: {str(e)[:60]}")
