#!/usr/bin/env python3
"""
daytrading_cycle.py — Bot 2 : DayTrading XAU/USD via Moonx CFD
Stratégie SMC/ICT :
  - Biais 1D : EMA 200 (au-dessus = achat seulement, en-dessous = vente seulement)
  - Confirmation 4H : EMA21 > EMA55 (achat) ou EMA21 < EMA55 (vente)
  - Entrée : prix dans un FVG/Imbalance en zone Discount (<0.5 Fibonacci) pour achat
              ou en zone Premium (>0.5 Fibonacci) pour vente
  - SL : 1% du prix d'entrée | TP : 2% du prix d'entrée (ratio 1:2)
  - Marge : 10 USDT × 10x levier = exposition 100 USDT par trade
  - Aucun trade si conditions non réunies (retourne NO_TRADE avec raison)
Lancé par GitHub Actions toutes les 4 heures.
"""

import os
import json
import math
import requests
import traceback
import urllib3
from datetime import datetime, timezone

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuration ─────────────────────────────────────────────────────────────
MOONX_TOKEN = os.environ["MOONX_API_KEY"]
MOONX_URL   = "https://api.moon-x.io/mcp"
MOONX_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Mcp-Protocol-Version": "2024-11-05",
}

TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PAIR_ID       = "XAUUSD"
LEVERAGE      = 10
MARGIN_USDT   = 10.0    # marge par trade (USDT)
SL_PCT        = 1.0     # stop-loss en % du prix d'entrée
TP_PCT        = 2.0     # take-profit en % (ratio 1:2)
EMA200_PERIOD = 200
FIBO_LOOKBACK = 50      # bougies 4H pour swing Fibonacci
FVG_LOOKBACK  = 30      # bougies 4H pour chercher les FVG

STATE_FILE = "state_daytrading.json"


# ─── Moonx REST API (JSON-RPC 2.0) ────────────────────────────────────────────
def _moonx_call(method_name, arguments):
    payload = {
        "jsonrpc": "2.0",
        "method":  "tools/call",
        "params":  {"name": method_name, "arguments": arguments},
        "id": 1
    }
    resp = requests.post(
        f"{MOONX_URL}?token={MOONX_TOKEN}",
        json=payload,
        headers=MOONX_HEADERS,
        timeout=20,
        verify=False,
    )
    resp.raise_for_status()
    data = resp.json()
    print(f"[DEBUG:{method_name}] top-level keys: {list(data.keys())}")

    if "error" in data:
        raise RuntimeError(f"Moonx error [{method_name}]: {data['error']}")

    result = data.get("result", data)
    print(f"[DEBUG:{method_name}] result type: {type(result).__name__}, "
          f"keys: {list(result.keys()) if isinstance(result, dict) else 'N/A (list)'}")

    # Cas 1 : result est déjà une liste (candles, positions...)
    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        # Cas 2 : format MCP → result.content[0].text contient du JSON sérialisé
        if "content" in result:
            text = result["content"][0].get("text", "null")
            print(f"[DEBUG:{method_name}] content text (200c): {text[:200]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}

        # Cas 3 : données directement dans un sous-champ courant
        for key in ("candles", "data", "bars", "positions", "price"):
            if key in result:
                return result[key]

    # Retour brut pour parsing manuel côté appelant
    return result


def get_candles(symbol, interval, limit=200):
    raw = _moonx_call("get_candles", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    })
    # Si la réponse est encore un dict, on cherche les bougies à l'intérieur
    if isinstance(raw, dict):
        print(f"[DEBUG:get_candles] dict reçu, clés : {list(raw.keys())}")
        for key in ("candles", "data", "bars", "ohlcv", "result"):
            if key in raw and isinstance(raw[key], list):
                return raw[key]
        raise RuntimeError(f"Impossible de trouver les bougies dans : {list(raw.keys())}")
    if not isinstance(raw, list):
        raise RuntimeError(f"Format bougies inattendu : {type(raw)}")
    return raw


def get_current_price():
    data = _moonx_call("get_price", {"symbol": PAIR_ID})
    return float(data.get("price", 0))


def list_open_positions():
    result = _moonx_call("list_forex_positions", {})
    if isinstance(result, list):
        return result
    return result.get("positions", result.get("data", []))


def open_position(side, lots, sl, tp):
    return _moonx_call("open_forex_position", {
        "pairId":     PAIR_ID,
        "side":       side,
        "lots":       lots,
        "stopLoss":   round(sl, 2),
        "takeProfit": round(tp, 2),
    })


# ─── Telegram ─────────────────────────────────────────────────────────────────
def notify(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    print(f"[telegram] envoi → chat_id={TELEGRAM_CHAT_ID} token_prefix={TELEGRAM_TOKEN[:10]}...")
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"[telegram] réponse HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[telegram] erreur réseau: {e}")


# ─── Indicateurs techniques ───────────────────────────────────────────────────
def calc_ema(closes, period):
    """Calcule l'EMA (Exponential Moving Average) sur une liste de clôtures."""
    if len(closes) < period:
        return None
    k = 2.0 / (period + 1)
    # Initialisation avec SMA des `period` premières valeurs
    ema_val = sum(closes[:period]) / period
    for price in closes[period:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val


def _ohlc(candle):
    """Normalise une bougie en dict avec open/high/low/close flottants."""
    return {
        "open":  float(candle.get("open",  candle.get("o", 0))),
        "high":  float(candle.get("high",  candle.get("h", 0))),
        "low":   float(candle.get("low",   candle.get("l", 0))),
        "close": float(candle.get("close", candle.get("c", 0))),
    }


def detect_fvgs(candles, direction, lookback=30):
    """
    Détecte les Fair Value Gaps (FVG / Imbalances) récents.
    Bullish FVG  : candle[i].low > candle[i-2].high  → zone achat entre ces deux niveaux
    Bearish FVG  : candle[i].high < candle[i-2].low  → zone vente entre ces deux niveaux
    Retourne une liste de dicts {low, high, type}.
    """
    recent = [_ohlc(c) for c in candles[-lookback - 2:]]
    fvgs = []
    for i in range(2, len(recent)):
        if direction == "buy" and recent[i]["low"] > recent[i - 2]["high"]:
            fvgs.append({
                "low":  recent[i - 2]["high"],
                "high": recent[i]["low"],
                "type": "bullish",
            })
        elif direction == "sell" and recent[i]["high"] < recent[i - 2]["low"]:
            fvgs.append({
                "low":  recent[i]["high"],
                "high": recent[i - 2]["low"],
                "type": "bearish",
            })
    return fvgs


def price_in_fvg(price, fvgs):
    """Retourne (True, fvg) si le prix est dans un FVG actif, sinon (False, None)."""
    for fvg in reversed(fvgs):   # le plus récent en priorité
        if fvg["low"] <= price <= fvg["high"]:
            return True, fvg
    return False, None


def fibonacci_zones(candles, lookback):
    """
    Calcule les niveaux Fibonacci sur le swing high/low des `lookback` dernières bougies.
    Retourne swing_high, swing_low, fib50 (niveau 50% = frontière Discount/Premium).
    """
    recent = [_ohlc(c) for c in candles[-lookback:]]
    swing_high = max(c["high"] for c in recent)
    swing_low  = min(c["low"]  for c in recent)
    rng = swing_high - swing_low
    return {
        "swing_high": swing_high,
        "swing_low":  swing_low,
        "fib50":      swing_high - rng * 0.5,
        "fib618":     swing_high - rng * 0.618,
        "fib786":     swing_high - rng * 0.786,
    }


def calc_lots(entry_price):
    """
    Volume en lots pour XAUUSD (CFD Moonx).
    Hypothèse : 1 lot = 1 oz d'or (micro-lot).
    exposition = marge × levier → lots = exposition / prix.
    Minimum : 0.01 lots.
    """
    exposure = MARGIN_USDT * LEVERAGE
    lots = exposure / entry_price
    return max(0.01, round(lots, 2))


# ─── État persistant ──────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_position_id": None, "last_trade_ts": None, "total_trades": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Cycle principal ──────────────────────────────────────────────────────────
def run():
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"[{now}] Cycle DayTrading XAU/USD démarré")
    print(f"{'='*60}")

    state = load_state()

    # ── 0. Vérifier position déjà ouverte ──────────────────────────────────
    open_pos = list_open_positions()
    xau_open = [p for p in open_pos
                if PAIR_ID.upper() in str(p.get("pairId", p.get("symbol", ""))).upper()]
    if xau_open:
        print(f"[{now}] Position {PAIR_ID} déjà ouverte ({len(xau_open)}) → NO_TRADE")
        notify(f"⏸ <b>XAU/USD Bot 2</b> | {now}\nPosition déjà ouverte — attente clôture.")
        return {"action": "NO_TRADE", "reason": "position_already_open"}

    # ── 1. Biais 1D (EMA 200) ──────────────────────────────────────────────
    candles_1d = get_candles(PAIR_ID, "1d", 220)
    closes_1d  = [float(c.get("close", c.get("c", 0))) for c in candles_1d]
    if len(closes_1d) < EMA200_PERIOD:
        print(f"[{now}] Bougies 1D insuffisantes ({len(closes_1d)}) → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_1d_candles"}

    ema200_1d = calc_ema(closes_1d, EMA200_PERIOD)
    close_1d  = closes_1d[-1]
    bias_bull = close_1d > ema200_1d
    bias_bear = close_1d < ema200_1d
    bias_str  = "HAUSSIER 🟢" if bias_bull else "BAISSIER 🔴"
    print(f"[1D] Close={close_1d:.2f}  EMA200={ema200_1d:.2f}  Biais={bias_str}")

    # ── 2. Confirmation 4H (EMA 21 / 55) ──────────────────────────────────
    candles_4h = get_candles(PAIR_ID, "4h", 110)
    closes_4h  = [float(c.get("close", c.get("c", 0))) for c in candles_4h]
    if len(closes_4h) < 60:
        print(f"[{now}] Bougies 4H insuffisantes → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_4h_candles"}

    ema21_4h  = calc_ema(closes_4h, 21)
    ema55_4h  = calc_ema(closes_4h, 55)
    trend_bull = ema21_4h > ema55_4h
    trend_bear = ema21_4h < ema55_4h
    price      = closes_4h[-1]
    print(f"[4H] Price={price:.2f}  EMA21={ema21_4h:.2f}  EMA55={ema55_4h:.2f}")

    # ── 3. Alignement biais 1D + tendance 4H ──────────────────────────────
    if bias_bull and trend_bull:
        direction = "buy"
    elif bias_bear and trend_bear:
        direction = "sell"
    else:
        print(f"[{now}] Biais 1D/4H non alignés → NO_TRADE")
        notify(
            f"⏳ <b>XAU/USD Bot 2 — NO TRADE</b> | {now}\n\n"
            f"❌ Biais 1D/4H non alignés\n"
            f"• 1D : {bias_str} (Close={close_1d:.0f} / EMA200={ema200_1d:.0f})\n"
            f"• 4H : EMA21={ema21_4h:.0f} {'>' if trend_bull else '<'} EMA55={ema55_4h:.0f}\n"
            f"→ Les deux timeframes doivent pointer dans le même sens."
        )
        return {"action": "NO_TRADE", "reason": "bias_misalignment_1d_4h"}

    print(f"[{now}] Direction validée : {direction.upper()}")

    # ── 4. Fibonacci — zone Discount/Premium ────────────────────────────────
    fib = fibonacci_zones(candles_4h, FIBO_LOOKBACK)
    print(f"[Fib] SwingH={fib['swing_high']:.2f}  SwingL={fib['swing_low']:.2f}  "
          f"Fib50={fib['fib50']:.2f}")

    if direction == "buy" and price > fib["fib50"]:
        print(f"[{now}] Prix {price:.2f} > Fib50 {fib['fib50']:.2f} → pas en Discount → NO_TRADE")
        notify(
            f"⏳ <b>XAU/USD Bot 2 — NO TRADE</b> | {now}\n\n"
            f"❌ Prix hors zone Discount\n"
            f"• Biais : {bias_str}\n"
            f"• Prix actuel : {price:.2f}\n"
            f"• Fib 50% (Discount/Premium) : {fib['fib50']:.2f}\n"
            f"→ Attente retour en zone Discount (<{fib['fib50']:.0f})."
        )
        return {"action": "NO_TRADE", "reason": "price_not_in_discount_zone"}

    if direction == "sell" and price < fib["fib50"]:
        print(f"[{now}] Prix {price:.2f} < Fib50 {fib['fib50']:.2f} → pas en Premium → NO_TRADE")
        notify(
            f"⏳ <b>XAU/USD Bot 2 — NO TRADE</b> | {now}\n\n"
            f"❌ Prix hors zone Premium\n"
            f"• Biais : {bias_str}\n"
            f"• Prix actuel : {price:.2f}\n"
            f"• Fib 50% (Discount/Premium) : {fib['fib50']:.2f}\n"
            f"→ Attente retour en zone Premium (>{fib['fib50']:.0f})."
        )
        return {"action": "NO_TRADE", "reason": "price_not_in_premium_zone"}

    zone_str = "Discount ✅" if direction == "buy" else "Premium ✅"
    print(f"[{now}] Zone Fibonacci : {zone_str}")

    # ── 5. FVG / Imbalance ─────────────────────────────────────────────────
    fvgs = detect_fvgs(candles_4h, direction, FVG_LOOKBACK)
    in_fvg, matched_fvg = price_in_fvg(price, fvgs)
    if not in_fvg:
        print(f"[{now}] Aucun FVG actif pour {direction} au prix {price:.2f} → NO_TRADE")
        notify(
            f"⏳ <b>XAU/USD Bot 2 — NO TRADE</b> | {now}\n\n"
            f"❌ Aucun FVG/Imbalance actif\n"
            f"• Biais : {bias_str} | Zone : {zone_str}\n"
            f"• Prix actuel : {price:.2f}\n"
            f"→ Attente d'un FVG en zone {zone_str}."
        )
        return {"action": "NO_TRADE", "reason": "no_active_fvg"}

    print(f"[FVG] Match : {matched_fvg['low']:.2f} – {matched_fvg['high']:.2f} ({matched_fvg['type']})")

    # ── 6. Calcul SL / TP (1:2) ────────────────────────────────────────────
    if direction == "buy":
        sl = round(price * (1 - SL_PCT / 100), 2)
        tp = round(price * (1 + TP_PCT / 100), 2)
    else:
        sl = round(price * (1 + SL_PCT / 100), 2)
        tp = round(price * (1 - TP_PCT / 100), 2)

    lots = calc_lots(price)
    print(f"[Trade] {direction.upper()} {lots} lots | Entry={price:.2f} | SL={sl} | TP={tp}")

    # ── 7. Exécution de l'ordre ────────────────────────────────────────────
    result     = open_position(direction, lots, sl, tp)
    pos_id     = (result.get("positionId")
                  or result.get("id")
                  or result.get("position_id")
                  or "unknown")
    print(f"[Moonx] Réponse : {result}")

    # ── 8. Mise à jour de l'état ───────────────────────────────────────────
    state["last_position_id"] = pos_id
    state["last_trade_ts"]    = now
    state["total_trades"]     = state.get("total_trades", 0) + 1
    save_state(state)

    # ── 9. Notification Telegram ───────────────────────────────────────────
    emoji = "📈" if direction == "buy" else "📉"
    direction_fr = "ACHAT 🟢" if direction == "buy" else "VENTE 🔴"
    msg = (
        f"{emoji} <b>TRADE EXÉCUTÉ — XAU/USD (Bot 2)</b>\n\n"
        f"Direction : <b>{direction_fr}</b>\n"
        f"Entrée    : <b>{price:.2f} USD</b>\n"
        f"Stop-Loss : {sl:.2f} ({SL_PCT}%)\n"
        f"Take-Profit: {tp:.2f} ({TP_PCT}%)\n"
        f"Lots      : {lots} | Marge : {MARGIN_USDT} USDT | Levier : {LEVERAGE}x\n\n"
        f"━━ Analyse ━━\n"
        f"Biais 1D  : {bias_str} (EMA200={ema200_1d:.0f})\n"
        f"Trend 4H  : EMA21={ema21_4h:.0f} {'>' if trend_bull else '<'} EMA55={ema55_4h:.0f}\n"
        f"Zone Fib  : {zone_str} (Fib50={fib['fib50']:.0f})\n"
        f"FVG       : {matched_fvg['low']:.0f}–{matched_fvg['high']:.0f}\n\n"
        f"🤖 Claude DayTrading Bot | {now}"
    )
    notify(msg)

    return {
        "action": "TRADE",
        "direction": direction,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "lots": lots,
        "position_id": pos_id,
    }


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        result = run()
        print(f"\n[Résultat] {json.dumps(result, indent=2)}")
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[ERREUR] {exc}\n{tb}")
        err_msg = (
            f"❌ <b>Erreur Bot DayTrading XAU/USD</b>\n\n"
            f"<code>{type(exc).__name__}: {exc}</code>"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": err_msg, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            pass
        raise
