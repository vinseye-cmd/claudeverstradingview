import { supertrend } from "./indicators/supertrend.js";
import { trendState } from "./indicators/movingAverages.js";
import { fibonacciLevels, findSwing, inEntryZone } from "./indicators/fibonacci.js";

/**
 * Analyse complete d'un actif a partir de bougies OHLC.
 * @param {Array<{high:number, low:number, close:number}>} candles - ordre chronologique, la derniere = actuelle
 * @param {object} rules - rules.json charge
 * @param {{symbol:string, timeframe:string, style:string}} meta
 */
export function analyzeAsset(candles, rules, meta) {
  const { supertrend: stCfg, moving_averages: maCfg, fibonacci: fiboCfg } = rules.strategy_filters;

  const stSeries = supertrend(candles, stCfg.atr_period, stCfg.multiplier);
  const st = stSeries[stSeries.length - 1];

  const trend = trendState(candles, maCfg);

  const swing = findSwing(candles, fiboCfg.swing_lookback_bars);
  const direction = st?.trend === "up" ? "up" : "down";
  const levels = fibonacciLevels(swing.high, swing.low, direction);
  const price = candles[candles.length - 1].close;
  const fibo = inEntryZone(price, levels, fiboCfg.entry_zones);

  if (!st || !trend.ready) {
    return { ready: false };
  }

  // Coherence: Supertrend haussier + tendance MA haussiere + prix en zone Fibo => setup d'achat.
  // Inverse pour la vente. Sinon: pas de signal, juste des donnees.
  let setup = null;
  if (st.trend === "up" && trend.bias === "haussier" && fibo.inZone) {
    setup = "achat";
  } else if (st.trend === "down" && trend.bias === "baissier" && fibo.inZone) {
    setup = "vente";
  }

  const atr = Math.abs(price - st.value);
  const atrPct = (atr / price) * 100;

  const stopLossPct = rules.risk.default_stop_loss_pct;
  const rr = rules.risk.default_take_profit_rr;
  const stopLoss = setup === "achat"
    ? Number((price * (1 - stopLossPct / 100)).toFixed(6))
    : Number((price * (1 + stopLossPct / 100)).toFixed(6));
  const takeProfit = setup === "achat"
    ? Number((price + (price - stopLoss) * rr).toFixed(6))
    : Number((price - (stopLoss - price) * rr).toFixed(6));

  return {
    ready: true,
    setup,
    atrPct,
    signal: setup ? {
      symbol: meta.symbol,
      style: meta.style,
      timeframe: meta.timeframe,
      direction: setup,
      price,
      supertrend: st,
      trend,
      fibonacci: fibo,
      stopLoss,
      takeProfit
    } : null
  };
}
