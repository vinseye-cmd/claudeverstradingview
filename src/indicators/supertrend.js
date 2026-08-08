/**
 * Calcule l'ATR (Average True Range) classique.
 * @param {Array<{high:number, low:number, close:number}>} candles
 * @param {number} period
 */
function atr(candles, period) {
  const trs = [];
  for (let i = 1; i < candles.length; i++) {
    const c = candles[i];
    const prevClose = candles[i - 1].close;
    const tr = Math.max(
      c.high - c.low,
      Math.abs(c.high - prevClose),
      Math.abs(c.low - prevClose)
    );
    trs.push(tr);
  }
  const result = new Array(candles.length).fill(null);
  let sum = 0;
  for (let i = 0; i < trs.length; i++) {
    if (i < period) {
      sum += trs[i];
      if (i === period - 1) result[i + 1] = sum / period;
    } else {
      const prevAtr = result[i];
      result[i + 1] = (prevAtr * (period - 1) + trs[i]) / period;
    }
  }
  return result;
}

/**
 * Calcule le Supertrend sur une serie de bougies.
 * @param {Array<{high:number, low:number, close:number}>} candles
 * @param {number} period - defaut 10
 * @param {number} multiplier - defaut 3
 * @returns {Array<{value:number, trend:"up"|"down"}|null>}
 */
export function supertrend(candles, period = 10, multiplier = 3) {
  const atrValues = atr(candles, period);
  const result = new Array(candles.length).fill(null);

  let prevUpperBand = null;
  let prevLowerBand = null;
  let prevTrend = "up";
  let prevSupertrend = null;

  for (let i = 0; i < candles.length; i++) {
    const a = atrValues[i];
    if (a === null) continue;

    const c = candles[i];
    const mid = (c.high + c.low) / 2;
    let upperBand = mid + multiplier * a;
    let lowerBand = mid - multiplier * a;

    if (prevUpperBand !== null) {
      upperBand = (upperBand < prevUpperBand || candles[i - 1].close > prevUpperBand)
        ? upperBand : prevUpperBand;
      lowerBand = (lowerBand > prevLowerBand || candles[i - 1].close < prevLowerBand)
        ? lowerBand : prevLowerBand;
    }

    let trend = prevTrend;
    if (prevSupertrend !== null) {
      if (prevTrend === "up" && c.close < lowerBand) trend = "down";
      else if (prevTrend === "down" && c.close > upperBand) trend = "up";
    }

    const value = trend === "up" ? lowerBand : upperBand;

    result[i] = { value: Number(value.toFixed(6)), trend };

    prevUpperBand = upperBand;
    prevLowerBand = lowerBand;
    prevTrend = trend;
    prevSupertrend = value;
  }

  return result;
}
