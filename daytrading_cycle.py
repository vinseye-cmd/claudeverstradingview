#!/usr/bin/env python3
"""
daytrading_cycle.py — Bot 2 : DayTrading XAU/USD via Moonx CFD
Stratégie SMC/ICT multi-timeframe :
  - Biais 1D  : EMA20 (au-dessus = achat seulement, en-dessous = vente seulement)
  - Trend 4H  : EMA21 > EMA55 (achat) ou EMA21 < EMA55 (vente)
  - Entrée 1H : FVG/Imbalance en zone Discount (Fibonacci) pour achat
                ou en zone Premium (Fibonacci) pour vente
  - Sessions  : London (07h-12h UTC) + New York (13h-20h UTC) — Asian exclue
  - SL : ~2x ATR_1H (~0.8%) | TP : 1.6% (ratio 1:2)
  - Marge : 9 USDT x 5x levier reel Moonx = 45 USDT exposition (~0.01 lots)
Lancé par GitHub Actions toutes les heures de 07h à 20h UTC.
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
MARGIN_USDT   = 9.0     # marge par trade (USDT) — laisse $1 de buffer sur le wallet de $10
SL_PCT        = 0.8     # stop-loss ~2x ATR_1H (ATR 1H ≈ 0.4% du prix)
TP_PCT        = 1.6     # take-profit → ratio 1:2 maintenu
EMA_TREND_PERIOD = 20   # période EMA biais 1D
FIBO_LOOKBACK    = 30   # bougies 1H pour swing Fibonacci (~1.25 jours)
FVG_LOOKBACK     = 20   # bougies 1H pour chercher les FVG
MIN_1D_CANDLES   = 15   # minimum de bougies 1D requises
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

    # ── 0. Filtre session — London (07h-12h UTC) + NY (13h-20h UTC) ───────
    hour_utc = datetime.now(timezone.utc).hour
    if not (SESSION_START <= hour_utc < SESSION_END):
        print(f"[{now}] Hors session active ({hour_utc}h UTC, fenetre {SESSION_START}h-{SESSION_END}h) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "outside_session"}

    # ── 1. Vérifier position déjà ouverte ──────────────────────────────────
    open_pos = list_open_positions()
    xau_open = [p for p in open_pos
                if PAIR_ID.upper() in str(p.get("pairId", p.get("symbol", ""))).upper()]
    if xau_open:
        print(f"[{now}] Position {PAIR_ID} déjà ouverte ({len(xau_open)}) → NO_TRADE")
        notify(f"⏸ XAU/USD Bot 2 | {now}\nPosition deja ouverte — attente cloture.")
        return {"action": "NO_TRADE", "reason": "position_already_open"}

    # ── 1. Biais 1D (EMA 200) ──────────────────────────────────────────────
    candles_1d = get_candles(PAIR_ID, "1d", 50)
    closes_1d  = [float(c.get("close", c.get("c", 0))) for c in candles_1d]
    if len(closes_1d) < MIN_1D_CANDLES:
        msg = (f"⚠️ XAU/USD Bot 2 — Donnees insuffisantes | {now}\n\n"
               f"Seulement {len(closes_1d)} bougies 1D disponibles (minimum {MIN_1D_CANDLES}).\n"
               f"Moonx limite l'historique XAU/USD forex.")
        notify(msg)
        print(f"[{now}] Bougies 1D insuffisantes ({len(closes_1d)}) → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_1d_candles"}

    period_1d = min(EMA_TREND_PERIOD, len(closes_1d) - 2)
    ema200_1d = calc_ema(closes_1d, period_1d)
    print(f"[1D] Bougies disponibles={len(closes_1d)}, période EMA utilisée={period_1d}")
    close_1d  = closes_1d[-1]
    bias_bull = close_1d > ema200_1d
    bias_bear = close_1d < ema200_1d
    bias_str  = "HAUSSIER 🟢" if bias_bull else "BAISSIER 🔴"
    print(f"[1D] Close={close_1d:.2f}  EMA{period_1d}={ema200_1d:.2f}  Biais={bias_str}")

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
    print(f"[4H] EMA21={ema21_4h:.2f}  EMA55={ema55_4h:.2f}  trend={'BULL' if trend_bull else 'BEAR'}")

    # ── 3. Alignement biais 1D + tendance 4H ──────────────────────────────
    if bias_bull and trend_bull:
        direction = "buy"
    elif bias_bear and trend_bear:
        direction = "sell"
    else:
        print(f"[{now}] Biais 1D/4H non alignés → NO_TRADE")
        notify(
            f"⏳ XAU/USD Bot 2 — NO TRADE | {now}\n\n"
            f"Biais 1D/4H non alignes\n"
            f"• 1D : {bias_str} (Close={close_1d:.0f} / EMA{period_1d}={ema200_1d:.0f})\n"
            f"• 4H : EMA21={ema21_4h:.0f} {'>' if trend_bull else 'v'} EMA55={ema55_4h:.0f}\n"
            f"Les deux timeframes doivent pointer dans le meme sens."
        )
        return {"action": "NO_TRADE", "reason": "bias_misalignment_1d_4h"}

    print(f"[{now}] Direction validée : {direction.upper()}")

    # ── 4. Données 1H — prix d'entrée, Fibonacci et FVG ───────────────────
    candles_1h = get_candles(PAIR_ID, "1h", 100)
    closes_1h  = [float(c.get("close", c.get("c", 0))) for c in candles_1h]
    if len(closes_1h) < 30:
        print(f"[{now}] Bougies 1H insuffisantes ({len(closes_1h)}) → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_1h_candles"}
    price = closes_1h[-1]
    print(f"[1H] Price={price:.2f}  (bougies disponibles={len(closes_1h)})")

    # ── 5. Fibonacci 1H — zone Discount/Premium ─────────────────────────────
    fib = fibonacci_zones(candles_1h, FIBO_LOOKBACK)
    print(f"[Fib] SwingH={fib['swing_high']:.2f}  SwingL={fib['swing_low']:.2f}  "
          f"Fib50={fib['fib50']:.2f}")

    if direction == "buy" and price > fib["fib50"]:
        print(f"[{now}] Prix {price:.2f} > Fib50 {fib['fib50']:.2f} → pas en Discount → NO_TRADE")
        notify(
            f"⏳ XAU/USD Bot 2 — NO TRADE | {now}\n\n"
            f"Prix hors zone Discount\n"
            f"• Biais : {bias_str}\n"
            f"• Prix actuel : {price:.2f}\n"
            f"• Fib 50% : {fib['fib50']:.2f}\n"
            f"Attente retour en zone Discount (sous {fib['fib50']:.0f})."
        )
        return {"action": "NO_TRADE", "reason": "price_not_in_discount_zone"}

    if direction == "sell" and price < fib["fib50"]:
        print(f"[{now}] Prix {price:.2f} < Fib50 {fib['fib50']:.2f} → pas en Premium → NO_TRADE")
        notify(
            f"⏳ XAU/USD Bot 2 — NO TRADE | {now}\n\n"
            f"Prix hors zone Premium\n"
            f"• Biais : {bias_str}\n"
            f"• Prix actuel : {price:.2f}\n"
            f"• Fib 50% : {fib['fib50']:.2f}\n"
            f"Attente retour en zone Premium (au-dessus de {fib['fib50']:.0f})."
        )
        return {"action": "NO_TRADE", "reason": "price_not_in_premium_zone"}

    zone_str = "Discount ✅" if direction == "buy" else "Premium ✅"
    print(f"[{now}] Zone Fibonacci : {zone_str}")

    # ── 6. FVG / Imbalance 1H ─────────────────────────────────────────────
    fvgs = detect_fvgs(candles_1h, direction, FVG_LOOKBACK)
    in_fvg, matched_fvg = price_in_fvg(price, fvgs)
    if not in_fvg:
        print(f"[{now}] Aucun FVG actif pour {direction} au prix {price:.2f} → NO_TRADE")
        notify(
            f"⏳ XAU/USD Bot 2 — NO TRADE | {now}\n\n"
            f"Aucun FVG/Imbalance actif\n"
            f"• Biais : {bias_str} | Zone : {zone_str}\n"
            f"• Prix actuel : {price:.2f}\n"
            f"Attente d'un FVG en zone {zone_str}."
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
        f"Stop-Loss  : {sl:.2f} ({SL_PCT}%)\n"
        f"Take-Profit: {tp:.2f} ({TP_PCT}%)\n"
        f"Lots       : {lots} | Marge : {MARGIN_USDT} USDT | Levier : {LEVERAGE}x\n"
        f"Position ID: {pos_id}\n\n"
        f"-- Analyse --\n"
        f"Biais 1D : {bias_str} (EMA{period_1d}={ema200_1d:.0f})\n"
        f"Trend 4H : EMA21={ema21_4h:.0f} {'>' if trend_bull else 'v'} EMA55={ema55_4h:.0f}\n"
        f"Zone Fib : {zone_str} (Fib50={fib['fib50']:.0f}) — swing 1H\n"
        f"FVG 1H   : {matched_fvg['low']:.0f}-{matched_fvg['high']:.0f}\n\n"
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
