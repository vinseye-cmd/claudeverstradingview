#!/usr/bin/env python3
"""
daytrading_cycle.py — Bot 2 : DayTrading XAU/USD via Moonx CFD
Strategie 0.5 (Fibonacci 50%) — mode 24h/5j :
  - Timeframe unique : 5 minutes
  - Actif            : lundi-vendredi, toutes les 5 minutes sans restriction horaire
  - Entree           : prix au niveau Fibonacci 0.5 du swing (± 0.8%)
  - Direction        : swing low recent → BUY | swing high recent → SELL
  - SL               : niveau Fibonacci 1 (extreme du swing) + buffer 0.1%
  - TP               : niveau Fibonacci 0 (oppose du swing)
  - Une seule position a la fois
"""

import os
import json
import requests
import traceback
import urllib3
from datetime import datetime, timezone

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

# ─── Parametres de trading ─────────────────────────────────────────────────────
PAIR_ID        = "XAUUSD"
LEVERAGE_REAL  = 500   # levier reel Moonx XAU/USD (appliedLeverage dans l'API)
CONTRACT_SIZE  = 100   # 100 oz par lot (contractSize Moonx XAUUSD)
MARGIN_USDT    = 9.0   # marge cible → 9×500/(4500×100) ≈ 0.01 lots → marge reelle ~$9
MIN_LOTS       = 0.01  # lot minimum Moonx XAUUSD

# ─── Strategie 0.5 ─────────────────────────────────────────────────────────────
SWING_CANDLES   = 24    # bougies 1H pour trouver le swing H/L (24h de donnees fiables)
FIB_ZONE_PCT    = 0.008 # 0.8% — zone d'entree autour du niveau 0.5
MIN_SWING_RANGE = 0.003 # le swing doit etre >= 0.3% du prix pour etre valide
FIB_SL_BUFFER   = 0.001 # 0.1% buffer au-dela de l extreme du swing pour le SL
MIN_RISK_RATIO  = 1.1   # free_margin >= risque_reel × 1.1 (10% buffer anti-liquidation)

STATE_FILE = "state_daytrading.json"
DRY_RUN    = os.environ.get("DRY_RUN", "false").lower() == "true"


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
        print(f"[DEBUG:get_candles] dict recu, cles : {list(raw.keys())}")
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


def get_forex_trade_history(limit=5):
    result = _moonx_call("get_forex_trade_history", {"limit": limit})
    if isinstance(result, list):
        return result
    return result.get("trades", result.get("data", []))


def get_forex_free_margin():
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
        print(f"[telegram] reponse HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[telegram] erreur reseau: {e}")


# ─── Helpers OHLC ─────────────────────────────────────────────────────────────
def _ohlc(candle):
    return {
        "open":  float(candle.get("open",  candle.get("o", 0))),
        "high":  float(candle.get("high",  candle.get("h", 0))),
        "low":   float(candle.get("low",   candle.get("l", 0))),
        "close": float(candle.get("close", candle.get("c", 0))),
    }


# ─── Swing detection ──────────────────────────────────────────────────────────
def find_swing(candles, lookback):
    window = [_ohlc(c) for c in candles[-lookback:]]
    swing_high = max(c["high"] for c in window)
    swing_low  = min(c["low"]  for c in window)
    high_idx   = max(range(len(window)), key=lambda i: window[i]["high"])
    low_idx    = min(range(len(window)), key=lambda i: window[i]["low"])
    return swing_high, swing_low, high_idx, low_idx


# ─── Calcul des lots ──────────────────────────────────────────────────────────
def calc_lots(entry_price):
    # lots = (marge × levier) / (prix × contractSize)
    lots = (MARGIN_USDT * LEVERAGE_REAL) / (entry_price * CONTRACT_SIZE)
    lots = max(MIN_LOTS, round(lots, 2))
    margin_check = lots * entry_price * CONTRACT_SIZE / LEVERAGE_REAL
    print(f"[calc_lots] lots={lots} | marge reelle={margin_check:.2f} USDT | P&L par point={lots * CONTRACT_SIZE:.2f} USDT")
    return lots


# ─── Etat persistant ──────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_position_id": None, "last_trade_ts": None, "total_trades": 0}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Cycle principal — Strategie 0.5 24h/5j ───────────────────────────────────
def run():
    now_dt = datetime.now(timezone.utc)
    now    = now_dt.strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*60}")
    print(f"[{now}] Cycle Strategie 0.5 — XAU/USD 24h/5j")
    print(f"{'='*60}")

    state = load_state()

    # ── 0. Filtre week-end ─────────────────────────────────────────────────────
    if now_dt.weekday() >= 5:
        print(f"[{now}] Week-end — marche ferme → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "weekend_closed"}

    # ── 1. Heartbeat quotidien (une fois par jour a la premiere execution) ──────
    today_key = now_dt.strftime("%Y-%m-%d")
    send_heartbeat = state.get("last_heartbeat_day") != today_key

    # ── 2. Verifier position deja ouverte + notifier cloture ─────────────────
    open_pos   = list_open_positions()
    pair_clean = PAIR_ID.upper().replace("/", "")
    xau_open   = [p for p in open_pos
                  if pair_clean in str(p.get("pairId", p.get("symbol", ""))).upper().replace("/", "")]

    # Notifier si une position suivie vient de se fermer (SL, TP ou liquidation)
    last_pos_id = state.get("last_position_id")
    if last_pos_id and last_pos_id not in ("unknown", "DRY_RUN"):
        still_open = any(str(p.get("_id", p.get("id", ""))) == last_pos_id for p in xau_open)
        if not still_open:
            try:
                history = get_forex_trade_history(limit=5)
                closed = next((t for t in history if str(t.get("_id", "")) == last_pos_id), None)
                if closed:
                    pnl      = float(closed.get("pnl", 0))
                    reason   = closed.get("metadata", {}).get("autoCloseReason", closed.get("status", "?"))
                    entry_p  = closed.get("entryPrice", "?")
                    exit_p   = closed.get("exitPrice", "?")
                    emoji_r  = "✅" if pnl > 0 else "❌"
                    reason_fr = {"sl": "Stop-Loss atteint", "tp": "Take-Profit atteint",
                                 "liquidation": "LIQUIDATION (solde insuffisant)",
                                 "closed": "Fermeture manuelle"}.get(reason, reason)
                    notify(
                        f"{emoji_r} TRADE FERME — XAU/USD Bot 2\n\n"
                        f"Resultat  : {'GAIN' if pnl > 0 else 'PERTE'} {pnl:+.2f} USDT\n"
                        f"Raison    : {reason_fr}\n"
                        f"Entree    : {entry_p} | Sortie : {exit_p}\n"
                        f"Position ID : {last_pos_id}\n"
                        f"{now}"
                    )
            except Exception as e:
                print(f"[ClotureCheck] Erreur: {e}")
            state["last_position_id"] = None
            save_state(state)

    if xau_open:
        print(f"[{now}] Position {PAIR_ID} deja ouverte → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "position_already_open"}

    # ── 3. Verifier solde forex ────────────────────────────────────────────────
    free_margin = get_forex_free_margin()
    print(f"[Wallet] Free margin forex = {free_margin:.2f} USDT")
    if free_margin < MARGIN_USDT:
        print(f"[{now}] Solde forex insuffisant ({free_margin:.2f} USDT < {MARGIN_USDT}) → NO_TRADE")
        if send_heartbeat:
            state["last_heartbeat_day"] = today_key
            save_state(state)
            notify(
                f"Alerte XAU/USD Bot 2 — Solde insuffisant | {today_key}\n\n"
                f"Free margin forex : {free_margin:.2f} USDT\n"
                f"Minimum requis   : {MARGIN_USDT:.1f} USDT (0.01 lots)\n\n"
                f"Le bot ne peut pas trader.\n"
                f"Transferer des fonds vers le wallet Forex pour reprendre.\n"
                f"{now}"
            )
        return {"action": "NO_TRADE", "reason": "insufficient_forex_balance"}

    # ── 4. Recuperer les bougies ───────────────────────────────────────────────
    # Prix actuel depuis les dernieres bougies 5m (timestamp recents)
    candles_5m = get_candles(PAIR_ID, "5m", 5)
    if not candles_5m:
        print(f"[{now}] Bougies 5m indisponibles → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_5m_candles"}
    price = float(candles_5m[-1].get("close", candles_5m[-1].get("c", 0)))
    print(f"[5m] Prix actuel = {price:.2f}")

    # Bougies 1H pour le swing (fiables, 1 bougie = 1 heure) + tendance
    candles_1h = get_candles(PAIR_ID, "1h", 30)
    if len(candles_1h) < SWING_CANDLES + 5:
        print(f"[{now}] Bougies 1H insuffisantes ({len(candles_1h)}) → NO_TRADE")
        return {"action": "NO_TRADE", "reason": "insufficient_1h_candles"}

    # ── 5. Swing High / Swing Low → niveaux Fibonacci ─────────────────────────
    swing_high, swing_low, high_idx, low_idx = find_swing(candles_1h, SWING_CANDLES)
    swing_range     = swing_high - swing_low
    swing_range_pct = swing_range / price * 100

    print(f"[Swing] High={swing_high:.2f} (idx={high_idx}) | Low={swing_low:.2f} (idx={low_idx})")
    print(f"[Swing] Range={swing_range:.2f} ({swing_range_pct:.2f}%)")

    if swing_range_pct < MIN_SWING_RANGE * 100:
        print(f"[{now}] Swing trop petit ({swing_range_pct:.2f}%) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "swing_range_too_small"}

    fib_0  = swing_high
    fib_05 = (swing_high + swing_low) / 2
    fib_1  = swing_low

    print(f"[Fib] 0={fib_0:.2f} | 0.5={fib_05:.2f} | 1={fib_1:.2f}")

    # ── 6. Direction : low recent → BUY | high recent → SELL ─────────────────
    if low_idx > high_idx:
        direction    = "buy"
        direction_fr = "ACHAT"
        print(f"[Direction] Swing LOW le plus recent → BUY (rebond vers niveau 0)")
    else:
        direction    = "sell"
        direction_fr = "VENTE"
        print(f"[Direction] Swing HIGH le plus recent → SELL (retrace vers niveau 1)")

    # ── 6.5. Filtre de tendance 1H (court terme ~4H) ─────────────────────────
    trend_fr  = "inconnue"
    daily_fr  = "inconnue"
    try:
        price_4h_ago = float(candles_1h[-5].get("close", candles_1h[-5].get("c", price)))
        trend_up = price > price_4h_ago
        trend_fr = "haussiere" if trend_up else "baissiere"
        print(f"[Tendance 1H] Prix 4H ago={price_4h_ago:.2f} | Prix={price:.2f} | Tendance={trend_fr}")
        if direction == "buy" and not trend_up:
            print(f"[{now}] ACHAT contre tendance 1H baissiere → NO_TRADE")
            return {"action": "NO_TRADE", "reason": "trend_conflict_buy_downtrend_1h"}
        if direction == "sell" and trend_up:
            print(f"[{now}] VENTE contre tendance 1H haussiere → NO_TRADE")
            return {"action": "NO_TRADE", "reason": "trend_conflict_sell_uptrend_1h"}
    except Exception as e:
        print(f"[Tendance 1H] Impossible: {e} — on continue")

    # ── 6.6. Filtre de tendance DAILY (macro sur 5 jours) ────────────────────
    # Bloque BUY si or en baisse sur 5j (evite d'acheter dans un downtrend macro)
    try:
        candles_1d = get_candles(PAIR_ID, "1d", 7)
        if len(candles_1d) >= 6:
            price_5d_ago = float(candles_1d[-6].get("close", candles_1d[-6].get("c", price)))
            daily_up   = price > price_5d_ago
            daily_fr   = "haussiere" if daily_up else "baissiere"
            daily_move = (price - price_5d_ago) / price_5d_ago * 100
            print(f"[Tendance Daily] 5j ago={price_5d_ago:.2f} | Prix={price:.2f} | Trend={daily_fr} ({daily_move:+.2f}%)")
            if direction == "buy" and not daily_up:
                print(f"[{now}] ACHAT contre tendance daily baissiere ({daily_move:.2f}%) → NO_TRADE")
                return {"action": "NO_TRADE", "reason": "trend_conflict_buy_daily_downtrend"}
            if direction == "sell" and daily_up:
                print(f"[{now}] VENTE contre tendance daily haussiere ({daily_move:.2f}%) → NO_TRADE")
                return {"action": "NO_TRADE", "reason": "trend_conflict_sell_daily_uptrend"}
        else:
            print("[Tendance Daily] Pas assez de donnees → filtre ignore")
    except Exception as e:
        print(f"[Tendance Daily] Impossible: {e} — on continue")

    # ── 7. Prix au niveau 0.5 ? ────────────────────────────────────────────────
    dist_to_05     = abs(price - fib_05)
    dist_to_05_pct = dist_to_05 / price * 100

    print(f"[Fib 0.5] Distance = {dist_to_05:.2f} ({dist_to_05_pct:.3f}%) | seuil={FIB_ZONE_PCT*100:.1f}%")

    # ── Calcul SL/TP Fibonacci + risque reel avant heartbeat ─────────────────
    lots = calc_lots(price)
    if direction == "buy":
        sl = round(fib_1 * (1 - FIB_SL_BUFFER), 2)
        tp = round(fib_0, 2)
    else:
        sl = round(fib_0 * (1 + FIB_SL_BUFFER), 2)
        tp = round(fib_1, 2)

    sl_distance      = abs(price - sl)                       # pts entre entree et SL
    real_risk_usdt   = lots * CONTRACT_SIZE * sl_distance    # risque reel en USDT
    real_profit_usdt = lots * CONTRACT_SIZE * abs(tp - price)
    rr               = real_profit_usdt / real_risk_usdt if real_risk_usdt > 0 else 0
    min_free_needed  = real_risk_usdt * MIN_RISK_RATIO       # free_margin minimum

    print(f"[SL/TP] SL={sl:.2f} ({sl_distance:.1f}pts={real_risk_usdt:.2f}$) | TP={tp:.2f} ({abs(tp-price):.1f}pts={real_profit_usdt:.2f}$) | R/R=1:{rr:.2f}")
    print(f"[Capital] Free margin={free_margin:.2f} | Risque={real_risk_usdt:.2f}$ | Min requis={min_free_needed:.2f}$")

    # ── Heartbeat quotidien ────────────────────────────────────────────────────
    if send_heartbeat:
        state["last_heartbeat_day"] = today_key
        save_state(state)
        capital_ok = free_margin >= min_free_needed
        capital_msg = (
            f"Capital OK — trading actif"
            if capital_ok else
            f"Capital INSUFFISANT — transferer {max(0, min_free_needed - free_margin + 1):.0f}+ USDT pour trader"
        )
        notify(
            f"Bot XAU/USD actif | {today_key}\n\n"
            f"Prix : {price:.2f} USD\n"
            f"Fib 0 (High) : {fib_0:.2f}\n"
            f"Fib 0.5      : {fib_05:.2f} (cible entree)\n"
            f"Fib 1 (Low)  : {fib_1:.2f}\n"
            f"Distance 0.5 : {dist_to_05_pct:.2f}% | seuil {FIB_ZONE_PCT*100:.1f}%\n"
            f"Direction    : {direction_fr}\n"
            f"Tendance 1H  : {trend_fr} | Tendance Daily : {daily_fr}\n"
            f"Risque/trade : {real_risk_usdt:.2f} USDT (SL Fib 1) | Gain cible : {real_profit_usdt:.2f} USDT (TP Fib 0)\n"
            f"Wallet forex : {free_margin:.2f} USDT libre | Min requis : {min_free_needed:.0f} USDT\n"
            f"{capital_msg}\n"
            f"Analyses toutes les 5 min\n"
            f"{now}"
        )

    if dist_to_05_pct > FIB_ZONE_PCT * 100:
        print(f"[{now}] Prix ({price:.2f}) pas au niveau 0.5 ({fib_05:.2f}) → NO_TRADE silencieux")
        return {"action": "NO_TRADE", "reason": "price_not_at_fib_05"}

    # ── 8. Garde-fou capital : free_margin >= risque_reel × 1.1 ─────────────
    if free_margin < min_free_needed:
        amount_needed = round(min_free_needed - free_margin + 2, 0)
        print(f"[{now}] Capital insuffisant : {free_margin:.2f}$ < {min_free_needed:.2f}$ → NO_TRADE")
        notify(
            f"Alerte XAU/USD Bot 2 — Capital insuffisant | {today_key}\n\n"
            f"Signal Fib 0.5 detecte : {direction_fr} a {price:.2f} USD\n"
            f"SL Fibonacci : {sl:.2f} ({sl_distance:.0f} pts de l entree)\n"
            f"Risque reel  : {real_risk_usdt:.2f} USDT\n"
            f"Free margin  : {free_margin:.2f} USDT (minimum : {min_free_needed:.2f} USDT)\n\n"
            f"Transferer {amount_needed:.0f} USDT vers le wallet Forex pour que ce trade soit pris.\n"
            f"{now}"
        )
        return {"action": "NO_TRADE", "reason": "insufficient_capital_for_fib_sl"}

    print(f"[Trade] {direction.upper()} {lots} lots | Entry={price:.2f} | SL={sl} | TP={tp}")

    # ── 9. Execution de l'ordre (ou simulation DRY_RUN) ──────────────────────
    if DRY_RUN:
        print(f"[DRY_RUN] Signal {direction_fr} a {price:.2f} | SL={sl} | TP={tp} — aucun ordre reel")
        emoji = "📈" if direction == "buy" else "📉"
        actual_margin = round(lots * price / LEVERAGE_REAL, 2)
        notify(
            f"[SIMULATION] {emoji} SIGNAL detecte — XAU/USD Bot 2\n\n"
            f"Mode DRY_RUN actif — aucun ordre place\n\n"
            f"Direction  : {direction_fr}\n"
            f"Entree     : {price:.2f} USD (niveau 0.5)\n"
            f"Stop-Loss  : {sl:.2f} (Fib 1 — risque {real_risk_usdt:.2f} USDT)\n"
            f"Take-Profit: {tp:.2f} (Fib 0 — gain {real_profit_usdt:.2f} USDT)\n"
            f"R/R        : 1:{rr:.2f}\n"
            f"Tendance 1H : {trend_fr} | Daily : {daily_fr}\n\n"
            f"-- Fibonacci --\n"
            f"Niveau 0   : {fib_0:.2f} | 0.5 : {fib_05:.2f} | 1 : {fib_1:.2f}\n"
            f"Range swing: {swing_range:.2f} ({swing_range_pct:.2f}%)\n\n"
            f"Claude DayTrading Bot | {now}"
        )
        state["total_signals"] = state.get("total_signals", 0) + 1
        save_state(state)
        return {"action": "DRY_RUN_SIGNAL", "direction": direction, "entry": price, "sl": sl, "tp": tp}

    try:
        result = open_position(direction, lots, sl, tp)
    except RuntimeError as order_err:
        err_detail = str(order_err)
        print(f"[ERREUR] open_position rejete: {err_detail}")
        notify(
            f"ORDRE REJETE — XAU/USD Bot 2\n\n"
            f"Direction : {direction_fr}\n"
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

    # ── 10. Mise a jour de l'etat ─────────────────────────────────────────────
    state["last_position_id"] = pos_id
    state["last_trade_ts"]    = now
    state["total_trades"]     = state.get("total_trades", 0) + 1
    save_state(state)

    # ── 11. Notification Telegram ─────────────────────────────────────────────
    emoji = "📈" if direction == "buy" else "📉"
    actual_margin = round(lots * price * CONTRACT_SIZE / LEVERAGE_REAL, 2)
    sl_dist_pct   = sl_distance / price * 100
    tp_dist_pct   = abs(tp - price) / price * 100
    msg = (
        f"{emoji} TRADE EXECUTE — XAU/USD Bot 2 | Strategie 0.5\n\n"
        f"Direction  : {direction_fr}\n"
        f"Entree     : {price:.2f} USD (niveau 0.5)\n"
        f"Stop-Loss  : {sl:.2f} (Fib 1 — {sl_dist_pct:.2f}% — risque {real_risk_usdt:.2f} USDT)\n"
        f"Take-Profit: {tp:.2f} (Fib 0 — {tp_dist_pct:.2f}% — gain {real_profit_usdt:.2f} USDT)\n"
        f"R/R        : 1:{rr:.2f}\n"
        f"Lots       : {lots} | Marge : {actual_margin:.2f} USDT | Levier : {LEVERAGE_REAL}x\n\n"
        f"-- Fibonacci --\n"
        f"Niveau 0   : {fib_0:.2f} (Swing High)\n"
        f"Niveau 0.5 : {fib_05:.2f} (entree)\n"
        f"Niveau 1   : {fib_1:.2f} (Swing Low)\n"
        f"Range swing: {swing_range:.2f} ({swing_range_pct:.2f}%)\n\n"
        f"Position ID: {pos_id}\n"
        f"Claude DayTrading Bot | {now}"
    )
    notify(msg)

    return {
        "action":      "TRADE",
        "direction":   direction,
        "entry":       price,
        "fib_0":       fib_0,
        "fib_05":      fib_05,
        "fib_1":       fib_1,
        "sl":          sl,
        "tp":          tp,
        "lots":        lots,
        "rr":          round(rr, 2),
        "position_id": pos_id,
    }


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        result = run()
        print(f"\n[Resultat] {json.dumps(result, indent=2)}")
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
