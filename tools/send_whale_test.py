"""
Отправить тестовый whale-сигнал в веб-дашборд.

Использование:
  python tools/send_whale_test.py                        # 3x T3 BUY INIT
  python tools/send_whale_test.py 3 sell ABS 3           # 3x T3 SELL ABS
  python tools/send_whale_test.py 2 buy INIT 2           # 2x T2 BUY INIT
"""
import sys
import json
import urllib.request

PORT = 3001
ENDPOINT = f"http://127.0.0.1:{PORT}/api/whale-test"


def send(tier=3, direction="buy", classification="INIT", count=3):
    payload = json.dumps({
        "tier": tier,
        "direction": direction,
        "classification": classification,
        "count": count,
    }).encode()

    req = urllib.request.Request(
        ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        result = json.loads(resp.read())
        print("OK: sent {} signals to {} clients".format(result["signals"], result["sent_to"]))
        return result
    except Exception as e:
        print("ERROR: {}".format(e))
        return None


if __name__ == "__main__":
    tier = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    direction = sys.argv[2] if len(sys.argv) > 2 else "buy"
    classification = sys.argv[3] if len(sys.argv) > 3 else "INIT"
    count = int(sys.argv[4]) if len(sys.argv) > 4 else 3

    print("Sending {}x T{} {} {}...".format(count, tier, direction.upper(), classification))
    send(tier, direction, classification, count)
