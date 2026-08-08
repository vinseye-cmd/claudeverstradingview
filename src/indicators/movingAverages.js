/**
 * Moyenne mobile exponentielle.
 * @param {number[]} values
 * @param {number} period
 * @returns {Array<number|null>}
 */
export function ema(values, period) {
  const k = 2 / (period + 1);
  const result = new Array(values.length).fill(null);
  let prev = null;

  for (let i = 0; i < values.length; i++) {
    if (i < period - 1) continue;
    if (prev === null) {
      const seed = values.slice(i - period + 1, i + 1).reduce((a, b) => a + b, 0) / period;
      prev = seed;
      result[i] = seed;
      continue;
    }
    prev = values[i] * k + prev * (1 - k);
    result[i] = Number(prev.toFixed(6));
  }
  return result;
}

/**
 * Determine la tendance en croisant EMA rapide/lente, filtree par une EMA de fond (ex: 200).
 * @param {Array<{close:number}>} candles
 * @param {{fast:number, slow:number, trend_filter:number}} params
 */
export function trendState(candles, { fast = 21, slow = 55, trend_filter = 200 } = {}) {
  const closes = candles.map(c => c.close);
  const emaFast = ema(closes, fast);
  const emaSlow = ema(closes, slow);
  const emaTrend = ema(closes, trend_filter);

  const i = closes.length - 1;
  if (emaFast[i] === null || emaSlow[i] === null || emaTrend[i] === null) {
    return { ready: false };
  }

  const price = closes[i];
  const aboveTrendFilter = price > emaTrend[i];
  const bullishCross = emaFast[i] > emaSlow[i];

  let bias = "neutre";
  if (aboveTrendFilter && bullishCross) bias = "haussier";
  else if (!aboveTrendFilter && !bullishCross) bias = "baissier";

  return {
    ready: true,
    bias,
    emaFast: emaFast[i],
    emaSlow: emaSlow[i],
    emaTrendFilter: emaTrend[i],
    price
  };
}
