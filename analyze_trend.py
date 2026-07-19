import sys
sys.path.insert(0, '.')

import asyncio
import pandas as pd

async def main():
    from data.exchange_client import exchange_client
    await exchange_client.connect()

    for symbol in ['BTC/USDT', 'ETH/USDT']:
        print(f"\n{'='*60}")
        print(f"  {symbol} -- Recent 4 days (1h candles)")
        print(f"{'='*60}")

        df = await exchange_client.fetch_ohlcv(symbol, '1h', limit=100)
        df = df.reset_index()
        df['date'] = df['timestamp'].dt.date

        print(f"  Data range: {df['timestamp'].iloc[0]} to {df['timestamp'].iloc[-1]}")
        print(f"  Total candles: {len(df)}")

        unique_dates = sorted(df['date'].unique())
        print(f"  Available dates: {[str(d) for d in unique_dates]}")

        target_dates = unique_dates[-4:] if len(unique_dates) >= 4 else unique_dates
        print(f"  Analyzing: {[str(d) for d in target_dates]}")

        daily_data = []
        for d in target_dates:
            day_df = df[df['date'] == d]
            if day_df.empty:
                print(f"  {d}: NO DATA")
                continue
            o = day_df.iloc[0]['open']
            h = day_df['high'].max()
            l = day_df['low'].min()
            c = day_df.iloc[-1]['close']
            chg = ((c - o) / o) * 100
            direction = "BULLISH" if c > o else "BEARISH" if c < o else "FLAT"
            daily_data.append({'date': d, 'open': o, 'high': h, 'low': l, 'close': c, 'chg': chg, 'dir': direction})
            print(f"  {d}: O={o:.2f}  H={h:.2f}  L={l:.2f}  C={c:.2f}  | {chg:+.2f}%  {direction}")

        if len(daily_data) >= 2:
            first_open = daily_data[0]['open']
            last_close = daily_data[-1]['close']
            total_chg = ((last_close - first_open) / first_open) * 100
            highs = [d['high'] for d in daily_data]
            lows = [d['low'] for d in daily_data]
            print(f"\n  --- Overall Period ---")
            print(f"  Start: {first_open:.2f}  End: {last_close:.2f}  Change: {total_chg:+.2f}%")
            print(f"  Period High: {max(highs):.2f}  Period Low: {min(lows):.2f}")

            if abs(total_chg) < 1.0:
                trend = "RANGING"
            elif total_chg > 0:
                trend = "TRENDING UP"
            else:
                trend = "TRENDING DOWN"
            print(f"  Trend: {trend}")

            print(f"\n  --- First 3 days (BUY signal window) ---")
            sig_days = daily_data[:3]
            if sig_days:
                sig_first = sig_days[0]['open']
                sig_last = sig_days[-1]['close']
                sig_chg = ((sig_last - sig_first) / sig_first) * 100
                print(f"  Start: {sig_first:.2f}  End: {sig_last:.2f}  Change: {sig_chg:+.2f}%")
                for sd in sig_days:
                    print(f"    {sd['date']}: {sd['chg']:+.2f}% {sd['dir']}")
                if sig_chg < -1.0:
                    print(f"  >>> Price was DROPPING {sig_chg:+.2f}% -- explains why BUY signals hit SL")
                elif sig_chg > 1.0:
                    print(f"  >>> Price was RISING {sig_chg:+.2f}% -- BUY signals should have worked")
                else:
                    print(f"  >>> Price was roughly FLAT {sig_chg:+.2f}% -- choppy action")

asyncio.run(main())
