#!/usr/bin/env python3
"""
daytrading_cycle.py — Bot 2 : DayTrading XAU/USD via Moonx CFD
Stratégie multi-timeframe 2 niveaux :
  - Biais 1H  : EMA21 > EMA55 = achat | EMA21 < EMA55 = vente
  - Entrée 15m: EMA8 > EMA21 + pullback (achat) ou EMA8 < EMA21 + pullback (vente)
  - Sessions  : London (07h UTC) → NY close (20h UTC) — Asian + week-end exclus
  - SL 0.4% / TP 0.8% (ratio 1:2)
Lancé par GitHub Actions toutes les heures de 07h à 20h UTC, lundi-vendredi.
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
LEVERAGE      = 5       # levier RÉEL appliqué par Moonx sur XAU/USD CFD (vérifié empiriquement)
MARGIN_USDT   = 19.0    # marge par trade (USDT) — wallet forex $20, cible 0.02 lots
EMA_FAST         = 8    # EMA rapide 15min pour signal d'entrée
EMA_SLOW         = 21   # EMA lente 15min pour signal d'entrée
SWING_LOOKBACK   = 20   # bougies 1H à scanner pour trouver le swing (20h)
SWING_BUFFER_PCT = 0.10 # buffer au-delà du swing (évite le faux breakout)
MIN_SL_PCT       = 0.20 # SL minimum (plancher anti-bruit)
MAX_SL_PCT       = 1.20 # SL maximum (plafond gestion du risque)
MIN_LOTS         = 0.01 # lot minimum accepté par Moonx pour XAUUSD
SESSION_START    = 7    # heure UTC début session active (London open)
SESSION_END      = 20   # heure UTC fin session active (NY close)

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
    result = _moonx_call("open_forex_position", {
        "pairId":     PAIR_ID,
        "side":       side,
        "lots":       lots,
        "stopLoss":   round(sl, 2),
        "takeProfit": round(tp, 2),
    })
    print(f"[Moonx open_forex_position] reponse complete: {result}")

    # Détecter les erreurs dans la réponse Moonx (format texte libre)
    if isinstance(result, dict):
        raw_text = str(result.get("raw", ""))
        if raw_text and any(w in raw_text.lower() for w in
                            ("error", "fail", "invalid", "rejected", "insufficient",
                             "not enough", "minimum", "exceed")):
            raise RuntimeError(f"Moonx a rejete l'ordre: {raw_text[:500]}")

        # La réponse doit contenir un identifiant de position
        pos_id = (result.get("positionId") or result.get("id")
                  or result.get("position_id") or result.get("orderId")
                  or result.get("tradeId"))
        if not pos_id:
            raise RuntimeError(
                f"Position non creee — reponse sans ID de position: {result}"
            )

    return result


# ─── Telegram ─────────────────────────────────────────────────────────────────
def notify(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
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
    Volume en lots pour XAUUSD CFD (Moonx).
    Hypothèse Moonx : 1 lot = 1 oz (micro-lot) → exposition = marge × levier / prix.
    Avec 10 USDT × 10x = 100 USDT exposition → lots ≈ 0.02 au prix ~4400.
    """
    exposure = MARGIN_USDT * LEVERAGE
    lots = exposure / entry_price
    lots = max(MIN_LOTS, round(lots, 2))
    print(f"[calc_lots] exposition={exposure:.0f} USDT | prix={entry_price:.2f} | lots={lots}")
    return lots


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

    # ── 0. Filtre session — London/NY uniquement, pas de week-end ─────────
    now_dt   = datetime.now(timezone.utc)
    weekday  = now_dt.weekday()   # 0=Lun … 4=Ven, 5=Sam, 6=Dim
    hour_utc = now_dt.hour
    if weekday >= 5:
        print(f"[{now}] Week-end (jour={weekday}) — marché forex fermé → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "weekend_closed"}
    if not (SESSION_START <= hour_utc < SESSION_END):
        print(f"[{now}] Hors session active ({hour_utc}h UTC) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "outside_session"}

    # ── 1. Vérifier position déjà ouverte ──────────────────────────────────
    open_pos = list_open_positions()
    xau_open = [p for p in open_pos
                if PAIR_ID.upper() in str(p.get("pairId", p.get("symbol", ""))).upper()]
    if xau_open:
        print(f"[{now}] Position {PAIR_ID} déjà ouverte ({len(xau_open)}) → NO_TRADE")
        notify(f"⏸ XAU/USD Bot 2 | {now}\nPosition deja ouverte — attente cloture.")
        return {"action": "NO_TRADE", "reason": "position_already_open"}

    # ── 2. Biais directionnel 1H (EMA21 / EMA55) ──────────────────────────
    candles_1h = get_candles(PAIR_ID, "1h", 100)
    closes_1h  = [float(c.get("close", c.get("c", 0))) for c in candles_1h]
    if len(closes_1h) < 60:
        print(f"[{now}] Bougies 1H insuffisantes → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_1h_candles"}

    ema21_1h  = calc_ema(closes_1h, 21)
    ema55_1h  = calc_ema(closes_1h, 55)
    trend_bull = ema21_1h > ema55_1h
    trend_bear = ema21_1h < ema55_1h
    print(f"[1H] EMA21={ema21_1h:.2f}  EMA55={ema55_1h:.2f}  trend={'BULL' if trend_bull else 'BEAR'}")

    if trend_bull:
        direction = "buy"
    elif trend_bear:
        direction = "sell"
    else:
        print(f"[{now}] 1H EMA21 == EMA55 (transition) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "1h_ema_flat"}

    print(f"[{now}] Direction 1H : {direction.upper()}")

    # ── 4. Signal d'entrée 15min — EMA8 / EMA21 cross + pullback ──────────
    candles_15m = get_candles(PAIR_ID, "15m", 100)
    closes_15m  = [float(c.get("close", c.get("c", 0))) for c in candles_15m]
    if len(closes_15m) < 30:
        print(f"[{now}] Bougies 15m insuffisantes ({len(closes_15m)}) → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_15m_candles"}
    price     = closes_15m[-1]
    ema8_15m  = calc_ema(closes_15m, EMA_FAST)
    ema21_15m = calc_ema(closes_15m, EMA_SLOW)
    print(f"[15m] Price={price:.2f}  EMA{EMA_FAST}={ema8_15m:.2f}  EMA{EMA_SLOW}={ema21_15m:.2f}")

    # Achat  : EMA8 > EMA21 (momentum haussier) ET prix sous ou proche de l'EMA8 (pullback)
    # Vente  : EMA8 < EMA21 (momentum baissier) ET prix dessus ou proche de l'EMA8
    if direction == "buy":
        ema_cross_ok = ema8_15m > ema21_15m
        pullback_ok  = price <= ema8_15m * 1.002   # prix dans ≤0.2% au-dessus de l'EMA8
        entry_ok     = ema_cross_ok and pullback_ok
    else:
        ema_cross_ok = ema8_15m < ema21_15m
        pullback_ok  = price >= ema8_15m * 0.998
        entry_ok     = ema_cross_ok and pullback_ok

    if not entry_ok:
        cross_sym = ">" if ema8_15m > ema21_15m else "<"
        print(f"[{now}] EMA{EMA_FAST}({ema8_15m:.0f}) {cross_sym} EMA{EMA_SLOW}({ema21_15m:.0f}) | "
              f"pullback_ok={pullback_ok} → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "ema_15m_conditions_not_met"}

    signal_str = f"EMA{EMA_FAST}={ema8_15m:.0f} > EMA{EMA_SLOW}={ema21_15m:.0f} + pullback"
    print(f"[15m] Signal valide : {signal_str}")

    # ── 6. Calcul SL/TP sur swing 1H (ratio 1:2) ──────────────────────────
    lows_1h  = [float(c.get("low",  c.get("l",  c.get("close", c.get("c", 0))))) for c in candles_1h]
    highs_1h = [float(c.get("high", c.get("h",  c.get("close", c.get("c", 0))))) for c in candles_1h]

    if direction == "buy":
        swing_ref   = min(lows_1h[-SWING_LOOKBACK:-1])
        sl_raw      = swing_ref * (1 - SWING_BUFFER_PCT / 100)
        sl_dist_pct = (price - sl_raw) / price * 100
        sl_dist_pct = max(MIN_SL_PCT, min(MAX_SL_PCT, sl_dist_pct))
        sl = round(price * (1 - sl_dist_pct / 100), 2)
        tp = round(price + 2 * (price - sl), 2)
    else:
        swing_ref   = max(highs_1h[-SWING_LOOKBACK:-1])
        sl_raw      = swing_ref * (1 + SWING_BUFFER_PCT / 100)
        sl_dist_pct = (sl_raw - price) / price * 100
        sl_dist_pct = max(MIN_SL_PCT, min(MAX_SL_PCT, sl_dist_pct))
        sl = round(price * (1 + sl_dist_pct / 100), 2)
        tp = round(price - 2 * (sl - price), 2)

    print(f"[SL/TP] Swing ref={swing_ref:.2f} | dist={sl_dist_pct:.2f}% | SL={sl} | TP={tp}")

    lots = calc_lots(price)
    print(f"[Trade] {direction.upper()} {lots} lots | Entry={price:.2f} | SL={sl} | TP={tp}")

    # ── 7. Exécution de l'ordre ────────────────────────────────────────────
    try:
        result = open_position(direction, lots, sl, tp)
    except RuntimeError as order_err:
        err_detail = str(order_err)
        print(f"[ERREUR] open_position rejete: {err_detail}")
        notify(
            f"ORDRE REJETE — XAU/USD Bot 2\n\n"
            f"Direction : {direction.upper()}\n"
            f"Lots      : {lots} | SL={sl:.2f} | TP={tp:.2f}\n"
            f"Raison    : {err_detail[:300]}\n\n"
            f"Verifier les parametres Moonx (margin, lots min/max).\n"
            f"{now}"
        )
        return {"action": "ERROR", "reason": "order_rejected", "detail": err_detail}

    pos_id = (result.get("positionId")
              or result.get("id")
              or result.get("position_id")
              or result.get("tradeId")
              or "unknown")
    print(f"[Moonx] Position creee ID={pos_id}")

    # ── 8. Mise à jour de l'état ───────────────────────────────────────────
    state["last_position_id"] = pos_id
    state["last_trade_ts"]    = now
    state["total_trades"]     = state.get("total_trades", 0) + 1
    save_state(state)

    # ── 9. Notification Telegram ───────────────────────────────────────────
    emoji = "📈" if direction == "buy" else "📉"
    direction_fr = "ACHAT" if direction == "buy" else "VENTE"
    msg = (
        f"{emoji} TRADE EXECUTE — XAU/USD Bot 2\n\n"
        f"Direction  : {direction_fr}\n"
        f"Entree     : {price:.2f} USD\n"
        f"Stop-Loss  : {sl:.2f} ({sl_dist_pct:.2f}% — swing 1H)\n"
        f"Take-Profit: {tp:.2f} (ratio 1:2)\n"
        f"Lots       : {lots} | Marge : {MARGIN_USDT} USDT | Levier : {LEVERAGE}x\n"
        f"Position ID: {pos_id}\n\n"
        f"-- Analyse --\n"
        f"Biais 1H : EMA21={ema21_1h:.0f} {'>' if trend_bull else 'v'} EMA55={ema55_1h:.0f}\n"
        f"Signal   : {signal_str}\n\n"
        f"Claude DayTrading Bot | {now}"
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
            f"ERREUR Bot DayTrading XAU/USD\n\n"
            f"{type(exc).__name__}: {exc}"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage",
                json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": err_msg},
                timeout=10,
            )
        except Exception:
            pass
        raise
