/**
 * Calcule les niveaux de retracement de Fibonacci entre un swing high et un swing low.
 * @param {number} swingHigh
 * @param {number} swingLow
 * @param {"up"|"down"} direction - "up" = retracement d'une jambe haussiere (support), "down" = jambe baissiere (resistance)
 * @returns {Record<string, number>} niveaux cles -> prix
 */
export function fibonacciLevels(swingHigh, swingLow, direction = "up") {
  const range = swingHigh - swingLow;
  const ratios = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

  const levels = {};
  for (const r of ratios) {
    const price = direction === "up"
      ? swingHigh - range * r
      : swingLow + range * r;
    levels[r.toString()] = Number(price.toFixed(6));
  }
  return levels;
}

/**
 * Trouve le swing high/low sur les N dernieres bougies.
 * @param {Array<{high:number, low:number}>} candles
 * @param {number} lookback
 */
export function findSwing(candles, lookback = 100) {
  const slice = candles.slice(-lookback);
  const high = Math.max(...slice.map(c => c.high));
  const low = Math.min(...slice.map(c => c.low));
  return { high, low };
}

/**
 * Determine si le prix actuel est dans une zone d'entree Fibonacci donnee (+/- tolerance).
 * @param {number} price
 * @param {Record<string, number>} levels
 * @param {number[]} zones - ex: [0.5, 0.618, 0.786]
 * @param {number} tolerancePct - tolerance en % du prix
 */
export function inEntryZone(price, levels, zones, tolerancePct = 0.15) {
  for (const z of zones) {
    const level = levels[z.toString()];
    if (level === undefined) continue;
    const tolerance = price * (tolerancePct / 100);
    if (Math.abs(price - level) <= tolerance) {
      return { inZone: true, level: z, price: level };
    }
  }
  return { inZone: false };
}
