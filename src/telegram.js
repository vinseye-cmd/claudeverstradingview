import fetch from "node-fetch";

const API_BASE = "https://api.telegram.org";

export async function sendTelegramMessage(text) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = process.env.TELEGRAM_CHAT_ID;

  if (!token || !chatId) {
    console.error("[telegram] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant dans .env — message non envoye.");
    return { ok: false, error: "config_manquante" };
  }

  const url = `${API_BASE}/bot${token}/sendMessage`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      chat_id: chatId,
      text,
      parse_mode: "Markdown",
      disable_web_page_preview: true
    })
  });

  if (!res.ok) {
    const body = await res.text();
    console.error("[telegram] echec envoi:", res.status, body);
    return { ok: false, error: body };
  }
  return { ok: true };
}

/** Construit le message d'alerte formate a partir d'un signal analyse. */
export function formatSignalMessage(signal) {
  const {
    symbol, style, direction, timeframe, price,
    supertrend, trend, fibonacci, stopLoss, takeProfit, session
  } = signal;

  const arrow = direction === "achat" ? "🟢 ACHAT" : "🔴 VENTE";

  return [
    `${arrow} — *${symbol}* (${timeframe}, session ${session})`,
    `Style: ${style}`,
    `Prix: ${price}`,
    `Supertrend: ${supertrend.trend} (${supertrend.value})`,
    `Tendance MA: ${trend.bias}`,
    fibonacci?.inZone ? `Zone Fibo: ${fibonacci.level} @ ${fibonacci.price}` : null,
    `SL suggere: ${stopLoss}`,
    `TP suggere (R:R): ${takeProfit}`,
    "",
    "_Signal informatif — confirme et execute toi-meme._"
  ].filter(Boolean).join("\n");
}
