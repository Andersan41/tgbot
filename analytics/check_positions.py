"""Check current open positions on exchange."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchange.client import ExchangeClient
from config.settings import config

async def main():
    client = ExchangeClient(config.exchange)
    await client.initialize()
    
    positions = await client.fetch_positions()
    print("\n=== OPEN POSITIONS ===")
    for p in positions:
        if p.get('contracts', 0) and float(p.get('contracts', 0)) > 0:
            symbol = p.get('symbol', '?')
            side = p.get('side', '?')
            amount = p.get('contracts', 0)
            entry = p.get('entryPrice', 0)
            unrealized_pnl = p.get('unrealizedPnl', 0)
            notional = p.get('notional', 0)
            print(f"  {symbol} {side} | amount={amount} | entry={entry} | PnL={unrealized_pnl} | notional={notional}")
    
    if not any(float(p.get('contracts', 0)) > 0 for p in positions):
        print("  No open positions found")
    
    await client.close()

if __name__ == "__main__":
    asyncio.run(main())
