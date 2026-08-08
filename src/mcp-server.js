import "dotenv/config";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { loadRules, saveRules } from "./rulesEngine.js";
import { analyzeAsset } from "./analysis.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const LOG_PATH = path.join(__dirname, "..", "logs", "signals.log");

const server = new Server(
  { name: "claudeverstradingview", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler("tools/list", async () => ({
  tools: [
    {
      name: "get_rules",
      description: "Retourne la configuration active (rules.json): watchlist, sessions, filtres, risque.",
      inputSchema: { type: "object", properties: {} }
    },
    {
      name: "update_rules",
      description: "Met a jour rules.json (fusion partielle). Utilise pour changer watchlist/sessions/risque a la demande.",
      inputSchema: {
        type: "object",
        properties: { patch: { type: "object", description: "Objet a fusionner dans rules.json" } },
        required: ["patch"]
      }
    },
    {
      name: "get_recent_signals",
      description: "Retourne les N derniers signaux logues (envoyes ou filtres) par le serveur webhook.",
      inputSchema: {
        type: "object",
        properties: { limit: { type: "number", description: "Nombre de lignes, defaut 20" } }
      }
    },
    {
      name: "analyze_candles",
      description: "Lance l'analyse Fibonacci + Supertrend + MA sur une serie de bougies fournie manuellement.",
      inputSchema: {
        type: "object",
        properties: {
          symbol: { type: "string" },
          timeframe: { type: "string" },
          style: { type: "string" },
          candles: {
            type: "array",
            items: {
              type: "object",
              properties: {
                high: { type: "number" }, low: { type: "number" }, close: { type: "number" }
              },
              required: ["high", "low", "close"]
            }
          }
        },
        required: ["symbol", "candles"]
      }
    }
  ]
}));

server.setRequestHandler("tools/call", async (request) => {
  const { name, arguments: args } = request.params;

  if (name === "get_rules") {
    return { content: [{ type: "text", text: JSON.stringify(loadRules(), null, 2) }] };
  }

  if (name === "update_rules") {
    const current = loadRules();
    const merged = { ...current, ...args.patch };
    saveRules(merged);
    return { content: [{ type: "text", text: "rules.json mis a jour." }] };
  }

  if (name === "get_recent_signals") {
    if (!fs.existsSync(LOG_PATH)) {
      return { content: [{ type: "text", text: "Aucun signal logue pour le moment." }] };
    }
    const lines = fs.readFileSync(LOG_PATH, "utf-8").trim().split("\n").filter(Boolean);
    const limit = args?.limit || 20;
    const recent = lines.slice(-limit);
    return { content: [{ type: "text", text: recent.join("\n") }] };
  }

  if (name === "analyze_candles") {
    const rules = loadRules();
    const result = analyzeAsset(args.candles, rules, {
      symbol: args.symbol,
      timeframe: args.timeframe || "n/a",
      style: args.style || "n/a"
    });
    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }

  throw new Error(`Outil inconnu: ${name}`);
});

const transport = new StdioServerTransport();
await server.connect(transport);
