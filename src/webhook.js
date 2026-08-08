import "dotenv/config";
import express from "express";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { loadRules } from "./rulesEngine.js";
import { shouldAlert } from "./rulesEngine.js";
import { analyzeAsset } from "./analysis.js";
import { sendTelegramMessage, formatSignalMessage } from "./telegram.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const LOG_PATH = path.join(__dirname, "..", "logs", "signals.log");

const app = express();
app.use(express.json());

/**
 * Payload attendu depuis l'alerte TradingView (voir pinescript/README.md pour le message JSON a coller
 * dans la fenetre "Message" de l'alerte TradingView):
 * {
 *   "secret": "...",
 *   "symbol": "EURUSD",
 *   "timeframe": "15m",
 *   "style": "daytrading",
 *   "candles": [{high, low, close}, ...]   // fournies par ta strategie Pine via request.security ou un export externe
 * }
 *
 * NOTE IMPORTANTE: TradingView n'envoie PAS l'historique de bougies dans une alerte nativement.
 * Deux options realistes:
 *  1) Le webhook ne recoit que le signal (prix, direction) declenche par TA logique Pine deja calculee
 *     dans le script (Supertrend/MA/Fibo en Pine, cf. pinescript/strategy.pine) et ce serveur se contente
 *     de filtrer (session/volatilite) + notifier Telegram — c'est le mode implemente ici par defaut.
 *  2) Ce serveur va lui-meme chercher les bougies via une API de donnees de marche (ex: Moonx pour le crypto,
 *     une API forex) pour recalculer/confirmer — a brancher dans analysis.js si tu veux ce niveau.
 */
app.post("/webhook/tradingview", async (req, res) => {
  const body = req.body;

  if (body.secret !== process.env.WEBHOOK_SECRET) {
    return res.status(401).json({ ok: false, error: "secret_invalide" });
  }

  const rules = loadRules();
  const decision = shouldAlert(rules, { atrPct: body.atrPct });

  const logLine = JSON.stringify({ ts: new Date().toISOString(), body, decision }) + "\n";
  fs.appendFileSync(LOG_PATH, logLine);

  if (!decision.send) {
    return res.json({ ok: true, sent: false, reason: decision.reason });
  }

  const message = formatSignalMessage({
    symbol: body.symbol,
    style: body.style || "n/a",
    timeframe: body.timeframe || "n/a",
    direction: body.direction,
    price: body.price,
    supertrend: body.supertrend || { trend: body.direction === "achat" ? "up" : "down", value: body.price },
    trend: body.trend || { bias: body.direction === "achat" ? "haussier" : "baissier" },
    fibonacci: body.fibonacci,
    stopLoss: body.stopLoss ?? "-",
    takeProfit: body.takeProfit ?? "-",
    session: decision.session
  });

  const result = await sendTelegramMessage(message);
  res.json({ ok: true, sent: result.ok, session: decision.session });
});

app.get("/health", (_req, res) => res.json({ ok: true }));

const port = process.env.WEBHOOK_PORT || 3000;
app.listen(port, () => {
  console.log(`[webhook] en ecoute sur http://localhost:${port}/webhook/tradingview`);
  console.log("[webhook] expose ce port publiquement via ngrok/cloudflared pour que TradingView puisse l'atteindre.");
});
