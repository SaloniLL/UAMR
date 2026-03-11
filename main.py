import os
import time
import pandas as pd
import numpy as np
import requests
import hmac
import hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv
import warnings
warnings.filterwarnings('ignore')

load_dotenv()

# ================= CONFIGURATION =================
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')
USE_TESTNET = os.getenv('USE_TESTNET', 'false').lower() == 'true'
SYMBOL = os.getenv('TRADING_SYMBOL', 'BTCUSDT')
TIMEFRAME = os.getenv('TIMEFRAME', '15m')

RISK_PER_TRADE = float(os.getenv('RISK_PER_TRADE', 0.01))
ATR_PERIOD = int(os.getenv('ATR_PERIOD', 14))
ATR_MULTIPLIER = float(os.getenv('ATR_MULTIPLIER', 1.0))
MIN_UA_VOLUME_PER_SIDE = float(os.getenv('MIN_UA_VOLUME_PER_SIDE', 0.05))
MIN_UA_TOTAL_VOLUME = float(os.getenv('MIN_UA_TOTAL_VOLUME', 0.1))

TG_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TG_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
HEARTBEAT_INTERVAL_HOURS = float(os.getenv('HEARTBEAT_INTERVAL_HOURS', 6))
LAST_HEARTBEAT_FILE = 'last_heartbeat.txt'

if USE_TESTNET:
    BASE_URL = "https://testnet.binancefuture.com"
    print("⚠️ MODE: TESTNET")
else:
    BASE_URL = "https://fapi.binance.com"
    print("✅ MODE: LIVE")

# ================= HELPER FUNCTIONS =================

def send_telegram_message(message):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", 
                      json={"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def check_heartbeat():
    now = datetime.now()
    last_hb = None
    if os.path.exists(LAST_HEARTBEAT_FILE):
        try: last_hb = datetime.fromisoformat(open(LAST_HEARTBEAT_FILE).read().strip())
        except: pass
    if last_hb is None or (now - last_hb) > timedelta(hours=HEARTBEAT_INTERVAL_HOURS):
        open(LAST_HEARTBEAT_FILE, 'w').write(now.isoformat())
        return True
    return False

def get_interval_ms(interval):
    return {'1m': 60000, '3m': 180000, '5m': 300000, '15m': 900000, '30m': 1800000, '1h': 3600000}.get(interval, 900000)

def sign_payload(payload):
    query_string = '&'.join([f"{k}={v}" for k, v in payload.items()])
    signature = hmac.new(API_SECRET.encode('utf-8'), query_string.encode('utf-8'), hashlib.sha256).hexdigest()
    return query_string + f"&signature={signature}"

def request_public(endpoint, params={}):
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, params=params, timeout=10)
    data = resp.json()
    # Check for API error structure
    if isinstance(data, dict) and 'code' in data and data['code'] < 0:
        raise Exception(f"Binance API Error: {data['msg']} (Code: {data['code']})")
    return data

def request_private(endpoint, params={}, method='POST'):
    url = f"{BASE_URL}{endpoint}"
    params['timestamp'] = int(time.time() * 1000)
    query_string = sign_payload(params)
    headers = {'X-MBX-APIKEY': API_KEY}
    
    if method == 'POST':
        resp = requests.post(f"{url}?{query_string}", headers=headers, timeout=10)
    elif method == 'DELETE':
        resp = requests.delete(f"{url}?{query_string}", headers=headers, timeout=10)
    else:
        resp = requests.get(f"{url}?{query_string}", headers=headers, timeout=10)
        
    data = resp.json()
    
    # 🔍 CRITICAL ERROR CHECK
    if isinstance(data, dict) and 'code' in data and data['code'] < 0:
        raise Exception(f"Binance API Error: {data['msg']} (Code: {data['code']})")
    
    return data

# ================= LOGIC =================

def is_meaningful_ua(vol_buy, vol_sell):
    return vol_buy >= MIN_UA_VOLUME_PER_SIDE and vol_sell >= MIN_UA_VOLUME_PER_SIDE and (vol_buy + vol_sell) >= MIN_UA_TOTAL_VOLUME


def reconstruct_bar_footprint(symbol, timeframe):
    try:
        # 1. Get Klines
        klines = request_public('/fapi/v1/klines', {'symbol': symbol, 'interval': timeframe, 'limit': 2})
        
        # Safety Check: Did we get data?
        if not klines or len(klines) < 2:
            print("⚠️ No Kline data received.")
            return None
        
        last_closed = klines[-2]
        bar_open_ts = int(last_closed[0])
        bar_close_ts = bar_open_ts + get_interval_ms(timeframe)
        bar_high = float(last_closed[2])
        bar_low = float(last_closed[3])
        bar_close = float(last_closed[4])
        bar_range = bar_high - bar_low
        
        # 2. Get Recent Trades
        trades = request_public('/fapi/v1/trades', {'symbol': symbol, 'limit': 1000})
        
        # Safety Check: Did we get trades?
        if not trades or len(trades) == 0:
            print("⚠️ No recent trades received (Market might be quiet on Testnet).")
            return None

        # Filter to our specific bar window
        bar_trades = []
        for t in trades:
            # Safety Check: Ensure 'T' (time) exists in the trade object
            if 'T' not in t:
                continue 
            if bar_open_ts <= t['T'] < bar_close_ts:
                bar_trades.append(t)
        
        if not bar_trades:
            print(f"⚠️ No trades found in the specific {timeframe} window.")
            return None
        
        h_buy, h_sell, l_buy, l_sell = 0.0, 0.0, 0.0, 0.0
        tol = 0.05
        
        for t in bar_trades:
            # Safety Check: Ensure required keys exist
            if 'p' not in t or 'q' not in t or 'm' not in t:
                continue
                
            price = float(t['p'])
            qty = float(t['q'])
            is_sell = t['m'] 
            v_buy = 0.0 if is_sell else qty
            v_sell = qty if is_sell else 0.0
            
            if abs(price - bar_high) < tol: h_buy += v_buy; h_sell += v_sell
            if abs(price - bar_low) < tol: l_buy += v_buy; l_sell += v_sell
            
        return {'ohlc': {'high': bar_high, 'low': bar_low, 'close': bar_close, 'range': bar_range},
                'high': {'buy': h_buy, 'sell': h_sell}, 'low': {'buy': l_buy, 'sell': l_sell}}
                
    except Exception as e:
        print(f"❌ Error reconstructing footprint: {e}")
        return None

def calculate_atr(symbol, timeframe, period=14):
    klines = request_public('/fapi/v1/klines', {'symbol': symbol, 'interval': timeframe, 'limit': period+5})
    df = pd.DataFrame(klines, columns=['ts','o','h','l','c','v','cte','ca','conf','iv','cv','av','ig'])
    df[['h','l','c']] = df[['h','l','c']].astype(float)
    tr1 = df['h'] - df['l']
    tr2 = abs(df['h'] - df['c'].shift(1))
    tr3 = abs(df['l'] - df['c'].shift(1))
    tr = pd.concat([tr1,tr2,tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    return float(atr) if not pd.isna(atr) else 100.0

def execute_trade(direction, entry_price, sl_price, tp_price):
    try:
        acc = request_private('/fapi/v2/account', {}, 'GET')
        balance = 0.0
        for asset in acc['assets']:
            if asset['asset'] == 'USDT': balance = float(asset['walletBalance']); break
        
        risk_amt = balance * RISK_PER_TRADE
        stop_dist = abs(entry_price - sl_price)
        if stop_dist == 0: return False
        
        qty = risk_amt / stop_dist
        if qty < 5.0/entry_price: qty = 5.0/entry_price
        qty = round(qty, 3)
        if qty <= 0: return False
        
        side = 'SELL' if direction == 'SHORT' else 'BUY'
        order = request_private('/fapi/v1/order', {'symbol': SYMBOL, 'side': side, 'type': 'MARKET', 'quantity': qty}, 'POST')
        fill_price = float(order.get('avgPrice', entry_price))
        
        sl_side = 'BUY' if direction == 'SHORT' else 'SELL'
        request_private('/fapi/v1/order', {'symbol': SYMBOL, 'side': sl_side, 'type': 'STOP_MARKET', 'quantity': qty, 'stopPrice': sl_price, 'reduceOnly': 'true'}, 'POST')
        request_private('/fapi/v1/order', {'symbol': SYMBOL, 'side': sl_side, 'type': 'STOP_MARKET', 'quantity': qty, 'stopPrice': tp_price, 'reduceOnly': 'true'}, 'POST')
        
        msg = f"✅ **TRADE OPENED**\n\nSymbol: `{SYMBOL}`\nDir: `{direction}`\nEntry: `${fill_price:.2f}`\nSize: `{qty}`\nSL: `${sl_price:.2f}`\nTP: `${tp_price:.2f}`"
        send_telegram_message(msg)
        return True
    except Exception as e:
        send_telegram_message(f"❌ **EXECUTION FAILED**\n{str(e)}")
        return False

def main():
    print("--- 🤖 UA Bot Cycle Started ---")
    try:
        # 1. Check Position
        pos = request_private('/fapi/v2/positionRisk', {'symbol': SYMBOL}, 'GET')
        active = None
        for p in pos:
            if float(p['positionAmt']) != 0: active = p; break
        
        if active:
            side = "LONG" if float(active['positionAmt']) > 0 else "SHORT"
            print(f"ℹ️ Active position ({side}). Skipping.")
            return

        # 2. Heartbeat
        if check_heartbeat():
            acc = request_private('/fapi/v2/account', {}, 'GET')
            bal = next((float(a['walletBalance']) for a in acc['assets'] if a['asset']=='USDT'), 0.0)
            send_telegram_message(f"💓 **BOT HEARTBEAT**\n\nMode: {'TESTNET' if USE_TESTNET else 'LIVE'}\nBal: `${bal:.2f}`\nStatus: ✅ Running")
            print("❤️ Heartbeat sent.")

        # 3. Detect UA
        data = reconstruct_bar_footprint(SYMBOL, TIMEFRAME)
        if not data:
            print("⚠️ No footprint data.")
            return
            
        ohlc = data['ohlc']
        hv = data['high']
        lv = data['low']
        
        if ohlc['range'] == 0: return
        close_pct = (ohlc['close'] - ohlc['low']) / ohlc['range']
        
        signal = None
        if close_pct >= 0.75 and is_meaningful_ua(hv['buy'], hv['sell']):
            print(f"🔍 SHORT Signal!")
            signal = 'SHORT'
        elif close_pct <= 0.25 and is_meaningful_ua(lv['buy'], lv['sell']):
            print(f"🔍 LONG Signal!")
            signal = 'LONG'
            
        if signal:
            atr = calculate_atr(SYMBOL, TIMEFRAME, ATR_PERIOD)
            sl_dist = atr * ATR_MULTIPLIER
            entry = ohlc['close']
            
            if signal == 'SHORT':
                sl = entry + sl_dist
                tp = ohlc['low']
                if tp >= entry: tp = entry - (sl_dist * 0.5)
            else:
                sl = entry - sl_dist
                tp = ohlc['high']
                if tp <= entry: tp = entry + (sl_dist * 0.5)
                
            execute_trade(signal, entry, sl, tp)
        else:
            print("✅ No signal.")
            
    except Exception as e:
        err = f"💀 **CRITICAL ERROR**\n{str(e)}"
        print(err)
        send_telegram_message(err)

if __name__ == "__main__":
    main()