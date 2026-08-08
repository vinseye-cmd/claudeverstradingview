import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const RULES_PATH = path.join(__dirname, "..", "rules.json");
const EXAMPLE_PATH = path.join(__dirname, "..", "rules.example.json");

export function loadRules() {
  const p = fs.existsSync(RULES_PATH) ? RULES_PATH : EXAMPLE_PATH;
  return JSON.parse(fs.readFileSync(p, "utf-8"));
}

export function saveRules(rules) {
  fs.writeFileSync(RULES_PATH, JSON.stringify(rules, null, 2));
}

function toMinutes(hhmm) {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
}

/** Determine si l'heure UTC actuelle tombe dans une session active. */
export function isSessionActive(rules, now = new Date()) {
  const nowMin = now.getUTCHours() * 60 + now.getUTCMinutes();
  for (const [name, s] of Object.entries(rules.sessions)) {
    if (name === "off_hours_alerts") continue;
    if (!s.active) continue;
    const start = toMinutes(s.start);
    const end = toMinutes(s.end);
    const inRange = start < end ? (nowMin >= start && nowMin < end) : (nowMin >= start || nowMin < end);
    if (inRange) return { active: true, session: name };
  }
  return { active: false, session: null };
}

/** Filtre un volatilite (ATR% du prix) contre le seuil minimum configure. */
export function passesVolatilityFilter(rules, atrPct) {
  return atrPct >= rules.strategy_filters.min_volatility_atr_pct;
}

/** Decide si un signal doit partir sur Telegram. */
export function shouldAlert(rules, { atrPct, now = new Date() } = {}) {
  const session = isSessionActive(rules, now);
  if (!session.active && !rules.sessions.off_hours_alerts) {
    return { send: false, reason: "hors_session" };
  }
  if (atrPct !== undefined && !passesVolatilityFilter(rules, atrPct)) {
    return { send: false, reason: "volatilite_insuffisante" };
  }
  return { send: true, session: session.session };
}
