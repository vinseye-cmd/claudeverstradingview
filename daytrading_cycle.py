#!/usr/bin/env python3
"""
daytrading_cycle.py — Bot 2 : DayTrading XAU/USD via Moonx CFD
Stratégie 0.5 (Fibonacci 50%) :
  - Timeframe unique : 5 minutes
  - Sessions        : London (09h00 UTC) et New York (15h00 UTC)
  - Observation     : 15 premières minutes de chaque session → pas de position
  - Direction       : flux d'ouverture — grosses bougies haussières/baissières
  - Entrée          : retracement au niveau Fibonacci 0.5 + bougie englobante
  - SL              : niveau Fibonacci 1 (extrême du swing) + buffer
  - TP              : niveau Fibonacci 0 (opposé du swing)
Lancé toutes les 5 minutes de 09h à 16h UTC, lundi-vendredi.
"""

import os
import json
import requests
import traceback
import urllib3
from datetime import datetime, timezone, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuration Moonx / Telegram ───────────────────────────────────────────
MOONX_TOKEN = os.environ["MOONX_API_KEY"]
MOONX_URL   = "https://api.moon-x.io/mcp"
MOONX_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Mcp-Protocol-Version": "2024-11-05",
}

TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# ─── Paramètres de trading ─────────────────────────────────────────────────────
PAIR_ID     = "XAUUSD"
LEVERAGE    = 5       # levier RÉEL Moonx XAU/USD (vérifié empiriquement)
MARGIN_USDT = 9.0     # marge cible → 9×5/4650 ≈ 0.01 lots → marge réelle ~$9.30 (wallet $15)
MIN_LOTS    = 0.01    # lot minimum Moonx XAUUSD

# ─── Stratégie 0.5 ─────────────────────────────────────────────────────────────
SWING_CANDLES   = 40    # bougies 5min pour trouver le swing H/L (~3h20)
FIB_ZONE_PCT    = 0.003 # 0.3% — tolérance autour du niveau 0.5 pour déclencher l'entrée
FIB_SL_BUFFER   = 0.001 # 0.1% buffer au-delà du swing pour le SL (évite le faux stop)
MIN_SWING_RANGE = 0.002 # le swing doit être ≥ 0.2% du prix pour être valide

# Sessions de trading (UTC) : observation 15min puis trade
SESSIONS = [
    {"label": "London",   "open": (9, 0),  "trade": (9, 15),  "close": (10, 30)},
    {"label": "New York", "open": (15, 0), "trade": (15, 15), "close": (16, 30)},
]

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

    if isinstance(result, list):
        return result

    if isinstance(result, dict):
        if "content" in result:
            text = result["content"][0].get("text", "null")
            print(f"[DEBUG:{method_name}] content text (200c): {text[:200]}")
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"raw": text}
        for key in ("candles", "data", "bars", "positions", "price"):
            if key in result:
                return result[key]

    return result


def get_candles(symbol, interval, limit=200):
    raw = _moonx_call("get_candles", {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
    })
    if isinstance(raw, dict):
        print(f"[DEBUG:get_candles] dict reçu, clés : {list(raw.keys())}")
        for key in ("candles", "data", "bars", "ohlcv", "result"):
            if key in raw and isinstance(raw[key], list):
                return raw[key]
        raise RuntimeError(f"Impossible de trouver les bougies dans : {list(raw.keys())}")
    if not isinstance(raw, list):
        raise RuntimeError(f"Format bougies inattendu : {type(raw)}")
    return raw


def list_open_positions():
    result = _moonx_call("list_forex_positions", {})
    if isinstance(result, list):
        return result
    return result.get("positions", result.get("data", []))


def get_forex_free_margin():
    """Retourne le free margin du wallet forex (0 si wallet vide/erreur)."""
    try:
        result = _moonx_call("get_account_overview", {})
        if isinstance(result, dict):
            fw = result.get("forexWallet", {})
            return float(fw.get("freeMargin", fw.get("free_margin", 0)) or 0)
    except Exception:
        pass
    return 0.0


def open_position(side, lots, sl, tp):
    result = _moonx_call("open_forex_position", {
        "pairId":     PAIR_ID,
        "side":       side,
        "lots":       lots,
        "stopLoss":   round(sl, 2),
        "takeProfit": round(tp, 2),
    })
    print(f"[Moonx open_forex_position] reponse complete: {result}")

    if isinstance(result, dict):
        raw_text = str(result.get("raw", ""))
        if raw_text and any(w in raw_text.lower() for w in
                            ("error", "fail", "invalid", "rejected", "insufficient",
                             "not enough", "minimum", "exceed")):
            raise RuntimeError(f"Moonx a rejete l'ordre: {raw_text[:500]}")

        # Moonx renvoie { success: True, position: { _id: "...", ... } }
        pos_block = result.get("position") or {}
        pos_id = (result.get("positionId") or result.get("id")
                  or result.get("position_id") or result.get("orderId")
                  or result.get("tradeId")
                  or pos_block.get("_id") or pos_block.get("id"))
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


# ─── Helpers OHLC ─────────────────────────────────────────────────────────────
def _ohlc(candle):
    return {
        "open":  float(candle.get("open",  candle.get("o", 0))),
        "high":  float(candle.get("high",  candle.get("h", 0))),
        "low":   float(candle.get("low",   candle.get("l", 0))),
        "close": float(candle.get("close", candle.get("c", 0))),
    }


# ─── Stratégie 0.5 — détection swing & englobante ────────────────────────────
def find_swing(candles, lookback):
    """
    Trouve le Swing High et Swing Low sur les `lookback` dernières bougies 5min.
    Retourne (swing_high, swing_low, high_idx, low_idx) — indices dans la fenêtre.
    """
    window = [_ohlc(c) for c in candles[-lookback:]]
    swing_high = max(c["high"] for c in window)
    swing_low  = min(c["low"]  for c in window)
    high_idx   = max(range(len(window)), key=lambda i: window[i]["high"])
    low_idx    = min(range(len(window)), key=lambda i: window[i]["low"])
    return swing_high, swing_low, high_idx, low_idx


def is_bullish_engulfing(candles):
    """
    Bougie verte englobante : la bougie courante (verte) englobe la précédente (rouge).
    Confirme un retournement haussier au niveau 0.5.
    """
    if len(candles) < 2:
        return False
    prev = _ohlc(candles[-2])
    curr = _ohlc(candles[-1])
    return (
        curr["close"] > curr["open"] and    # bougie courante verte
        prev["close"] < prev["open"] and    # bougie précédente rouge
        curr["open"]  <= prev["close"] and  # ouverture sous le close précédent
        curr["close"] >= prev["open"]       # clôture au-dessus de l'open précédent
    )


def is_bearish_engulfing(candles):
    """
    Bougie rouge englobante : la bougie courante (rouge) englobe la précédente (verte).
    Confirme un retournement baissier au niveau 0.5.
    """
    if len(candles) < 2:
        return False
    prev = _ohlc(candles[-2])
    curr = _ohlc(candles[-1])
    return (
        curr["close"] < curr["open"] and    # bougie courante rouge
        prev["close"] > prev["open"] and    # bougie précédente verte
        curr["open"]  >= prev["close"] and  # ouverture au-dessus du close précédent
        curr["close"] <= prev["open"]       # clôture sous l'open précédent
    )


def get_active_session(now_dt):
    """
    Retourne (session_label, phase, open_dt) pour la session active.
    phase = 'observation' (15 premières min) ou 'trading'
    Retourne (None, None, None) si hors session.
    """
    for s in SESSIONS:
        open_h,  open_m  = s["open"]
        trade_h, trade_m = s["trade"]
        close_h, close_m = s["close"]

        open_dt  = now_dt.replace(hour=open_h,  minute=open_m,  second=0, microsecond=0)
        trade_dt = now_dt.replace(hour=trade_h, minute=trade_m, second=0, microsecond=0)
        close_dt = now_dt.replace(hour=close_h, minute=close_m, second=0, microsecond=0)

        if open_dt <= now_dt < trade_dt:
            return s["label"], "observation", open_dt
        if trade_dt <= now_dt <= close_dt:
            return s["label"], "trading", open_dt

    return None, None, None


# ─── Calcul des lots ──────────────────────────────────────────────────────────
def calc_lots(entry_price):
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


# ─── Cycle principal — Stratégie 0.5 ──────────────────────────────────────────
def run():
    now_dt = datetime.now(timezone.utc)
    now    = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"[{now}] Cycle Stratégie 0.5 — XAU/USD")
    print(f"{'='*60}")

    state = load_state()

    # ── 0. Filtre week-end ─────────────────────────────────────────────────────
    if now_dt.weekday() >= 5:
        print(f"[{now}] Week-end — marché fermé → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "weekend_closed"}

    # ── 1. Vérifier la session active ─────────────────────────────────────────
    session_label, phase, session_open_dt = get_active_session(now_dt)
    if not session_label:
        print(f"[{now}] Hors session London/NY → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "outside_session"}

    if phase == "observation":
        mins = int((now_dt - session_open_dt).total_seconds() / 60)
        print(f"[{now}] Session {session_label} — observation ({mins}min/15min) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "observation_phase"}

    print(f"[{now}] Session {session_label} — phase TRADING")

    # ── 2. Vérifier position déjà ouverte ─────────────────────────────────────
    open_pos   = list_open_positions()
    pair_clean = PAIR_ID.upper().replace("/", "")
    xau_open   = [p for p in open_pos
                  if pair_clean in str(p.get("pairId", p.get("symbol", ""))).upper().replace("/", "")]
    if xau_open:
        print(f"[{now}] Position {PAIR_ID} déjà ouverte → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "position_already_open"}

    # ── 3. Vérifier solde forex ────────────────────────────────────────────────
    free_margin = get_forex_free_margin()
    print(f"[Wallet] Free margin forex = {free_margin:.2f} USDT")
    if free_margin < MARGIN_USDT:
        print(f"[{now}] Solde forex insuffisant ({free_margin:.2f} USDT < {MARGIN_USDT}) → NO_TRADE")
        if free_margin < 1.0:
            notify(
                f"Alerte XAU/USD Bot 2 — Wallet forex vide | {now}\n\n"
                f"Free margin : {free_margin:.2f} USDT\n"
                f"Transferer des fonds depuis Spot ou Futures vers le wallet Forex."
            )
        return {"action": "NO_TRADE", "reason": "insufficient_forex_balance"}

    # ── 4. Récupérer les bougies 5min ─────────────────────────────────────────
    candles_5m = get_candles(PAIR_ID, "5m", 120)
    if len(candles_5m) < SWING_CANDLES + 5:
        print(f"[{now}] Bougies 5m insuffisantes ({len(candles_5m)}) → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_5m_candles"}

    price = float(candles_5m[-1].get("close", candles_5m[-1].get("c", 0)))
    print(f"[5m] Prix actuel = {price:.2f}")

    # ── 5. Trouver le Swing High / Swing Low (Fibonacci 0 et 1) ───────────────
    swing_high, swing_low, high_idx, low_idx = find_swing(candles_5m, SWING_CANDLES)
    swing_range = swing_high - swing_low
    swing_range_pct = swing_range / price * 100

    print(f"[Swing] High={swing_high:.2f} (idx={high_idx}) | Low={swing_low:.2f} (idx={low_idx})")
    print(f"[Swing] Range={swing_range:.2f} ({swing_range_pct:.2f}%)")

    if swing_range_pct < MIN_SWING_RANGE * 100:
        print(f"[{now}] Swing trop petit ({swing_range_pct:.2f}%) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "swing_range_too_small"}

    # ── 6. Niveaux Fibonacci : 0 (haut), 0.5 (milieu), 1 (bas) ───────────────
    fib_0  = swing_high
    fib_05 = (swing_high + swing_low) / 2
    fib_1  = swing_low

    print(f"[Fib] 0={fib_0:.2f} | 0.5={fib_05:.2f} | 1={fib_1:.2f}")

    # ── 7. Déterminer la direction depuis le mouvement d'ouverture ─────────────
    # Le low le plus récent (low_idx > high_idx) → prix a chuté → BUY (rebond)
    # Le high le plus récent (high_idx > low_idx) → prix a monté → SELL (retrace)
    if low_idx > high_idx:
        direction = "buy"
        direction_fr = "ACHAT"
        print(f"[Direction] Swing LOW le plus récent → direction BUY (rebond au 0.5)")
    else:
        direction = "sell"
        direction_fr = "VENTE"
        print(f"[Direction] Swing HIGH le plus récent → direction SELL (retrace au 0.5)")

    # ── 8. Vérifier que le prix est au niveau 0.5 ─────────────────────────────
    dist_to_05     = abs(price - fib_05)
    dist_to_05_pct = dist_to_05 / price * 100

    print(f"[Fib 0.5] Distance prix/0.5 = {dist_to_05:.2f} ({dist_to_05_pct:.3f}%) | seuil={FIB_ZONE_PCT*100:.1f}%")

    if dist_to_05_pct > FIB_ZONE_PCT * 100:
        print(f"[{now}] Prix ({price:.2f}) pas au niveau 0.5 ({fib_05:.2f}) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "price_not_at_fib_05"}

    # ── 9. Confirmation par bougie englobante ──────────────────────────────────
    if direction == "buy":
        confirmed = is_bullish_engulfing(candles_5m)
        engulfing_type = "haussiere"
    else:
        confirmed = is_bearish_engulfing(candles_5m)
        engulfing_type = "baissiere"

    if not confirmed:
        print(f"[{now}] Pas de bougie englobante {engulfing_type} au 0.5 → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": f"no_{direction}_engulfing"}

    print(f"[Englobante] Bougie englobante {engulfing_type} confirmee au niveau 0.5")

    # ── 10. Calcul SL / TP sur niveaux Fibonacci ──────────────────────────────
    if direction == "buy":
        sl = round(fib_1 * (1 - FIB_SL_BUFFER), 2)   # légèrement sous fib_1
        tp = round(fib_0, 2)                            # objectif fib_0
    else:
        sl = round(fib_0 * (1 + FIB_SL_BUFFER), 2)   # légèrement au-dessus fib_0
        tp = round(fib_1, 2)                            # objectif fib_1

    sl_dist_pct = abs(price - sl) / price * 100
    tp_dist_pct = abs(tp - price) / price * 100
    rr = tp_dist_pct / sl_dist_pct if sl_dist_pct > 0 else 0

    print(f"[SL/TP] SL={sl:.2f} ({sl_dist_pct:.2f}%) | TP={tp:.2f} ({tp_dist_pct:.2f}%) | R/R={rr:.2f}")

    lots = calc_lots(price)
    print(f"[Trade] {direction.upper()} {lots} lots | Entry={price:.2f} | SL={sl} | TP={tp}")

    # ── 11. Exécution de l'ordre ───────────────────────────────────────────────
    try:
        result = open_position(direction, lots, sl, tp)
    except RuntimeError as order_err:
        err_detail = str(order_err)
        print(f"[ERREUR] open_position rejete: {err_detail}")
        notify(
            f"ORDRE REJETE — XAU/USD Bot 2\n\n"
            f"Direction : {direction_fr}\n"
            f"Session   : {session_label}\n"
            f"Lots      : {lots} | SL={sl:.2f} | TP={tp:.2f}\n"
            f"Raison    : {err_detail[:300]}\n\n"
            f"Verifier les parametres Moonx (margin, lots min/max).\n"
            f"{now}"
        )
        return {"action": "ERROR", "reason": "order_rejected", "detail": err_detail}

    pos_block = result.get("position") or {} if isinstance(result, dict) else {}
    pos_id = (result.get("positionId") or result.get("id")
              or result.get("position_id") or result.get("tradeId")
              or pos_block.get("_id") or pos_block.get("id")
              or "unknown") if isinstance(result, dict) else "unknown"
    print(f"[Moonx] Position creee ID={pos_id}")

    # ── 12. Mise à jour de l'état ──────────────────────────────────────────────
    state["last_position_id"] = pos_id
    state["last_trade_ts"]    = now
    state["total_trades"]     = state.get("total_trades", 0) + 1
    save_state(state)

    # ── 13. Notification Telegram ──────────────────────────────────────────────
    emoji = "📈" if direction == "buy" else "📉"
    msg = (
        f"{emoji} TRADE EXECUTE — XAU/USD Bot 2 | Strategie 0.5\n\n"
        f"Session    : {session_label}\n"
        f"Direction  : {direction_fr}\n"
        f"Entree     : {price:.2f} USD (niveau 0.5)\n"
        f"Stop-Loss  : {sl:.2f} (niveau 1 — {sl_dist_pct:.2f}%)\n"
        f"Take-Profit: {tp:.2f} (niveau 0 — {tp_dist_pct:.2f}%)\n"
        f"R/R        : 1:{rr:.2f}\n"
        f"Lots       : {lots} | Marge : {MARGIN_USDT} USDT | Levier : {LEVERAGE}x\n\n"
        f"-- Fibonacci --\n"
        f"Niveau 0   : {fib_0:.2f} (Swing High)\n"
        f"Niveau 0.5 : {fib_05:.2f} (entree)\n"
        f"Niveau 1   : {fib_1:.2f} (Swing Low)\n"
        f"Range swing: {swing_range:.2f} ({swing_range_pct:.2f}%)\n\n"
        f"Englobante : {engulfing_type} confirmee\n"
        f"Position ID: {pos_id}\n"
        f"Claude DayTrading Bot | {now}"
    )
    notify(msg)

    return {
        "action":    "TRADE",
        "direction": direction,
        "session":   session_label,
        "entry":     price,
        "fib_0":     fib_0,
        "fib_05":    fib_05,
        "fib_1":     fib_1,
        "sl":        sl,
        "tp":        tp,
        "lots":      lots,
        "rr":        round(rr, 2),
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
