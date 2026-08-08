# claudeverstradingview

Moteur de signaux : Pine Script (TradingView) → webhook → analyse Fibonacci/Supertrend/MA → alerte Telegram.

## Ce que ce projet fait

- Un indicateur Pine Script (`pinescript/strategy.pine`) calcule Supertrend + croisement EMA + zones de
  retracement Fibonacci directement dans TradingView, et declenche une alerte native quand un setup apparait.
- L'alerte TradingView envoie un webhook HTTP vers `src/webhook.js`, qui tourne sur ta machine (ou un VPS).
- `src/webhook.js` filtre le signal par session horaire (Asia/London/NY, voir `rules.json`) et par volatilite
  minimale, puis pousse un message Telegram formate avec SL/TP suggeres.
- `src/mcp-server.js` expose des outils MCP (`get_rules`, `update_rules`, `get_recent_signals`,
  `analyze_candles`) pour que je puisse consulter/ajuster la configuration pendant qu'on discute.

## Ce que ce projet ne fait PAS

- Aucune execution automatique de trade. `rules.json` a `autonomous_execution: false` en dur — ce projet ne
  place ni ne modifie d'ordres tout seul. Les alertes sont informatives ; tu executes toi-meme (via Moonx ou
  ton broker) apres confirmation.
- Aucun controle de l'appli mobile ou desktop TradingView. Techniquement impossible (pas d'API/debug
  accessible cote client). Tout passe par le Pine Script + webhook, qui fonctionne quel que soit l'appareil
  sur lequel tu regardes tes graphiques, puisque le calcul se fait cote serveur TradingView.

## Installation

1. **Copier la config**
   ```bash
   cp rules.example.json rules.json
   cp .env.example .env
   ```
   Remplis `.env` : token du bot Telegram (via @BotFather), ton `chat_id`, un `WEBHOOK_SECRET` (chaine
   aleatoire que toi seul connais).

2. **Installer les dependances**
   ```bash
   npm install
   ```

3. **Lancer le serveur webhook**
   - Windows : `scripts\start_webhook.bat`
   - macOS/Linux : `scripts/start_webhook.sh`

4. **Exposer le port publiquement** (TradingView doit pouvoir l'atteindre depuis internet) :
   ```bash
   ngrok http 3000
   ```
   Note l'URL `https://xxxx.ngrok.app/webhook/tradingview`.

5. **Configurer l'alerte dans TradingView**
   - Ajoute `pinescript/strategy.pine` comme indicateur sur ton graphique.
   - Clic droit → Creer une alerte → Condition = "Setup ACHAT" ou "Setup VENTE".
   - Webhook URL = l'URL ngrok de l'etape 4.
   - Le message JSON est deja rempli automatiquement par le script (variable `alertMessage`) —
     remplace juste `REMPLACE_PAR_TON_WEBHOOK_SECRET` par la valeur de `WEBHOOK_SECRET` dans ton `.env`.

6. **(Optionnel) Connecter le serveur MCP a Claude**
   Dans la config Claude Desktop/Code :
   ```json
   {
     "mcpServers": {
       "claudeverstradingview": {
         "command": "node",
         "args": ["/chemin/absolu/vers/claudeverstradingview/src/mcp-server.js"]
       }
     }
   }
   ```

## Verifier que ca marche

- `curl http://localhost:3000/health` → `{"ok":true}`
- Depuis TradingView, clique "Test" sur l'alerte → un message doit arriver sur Telegram en quelques secondes.
- `logs/signals.log` contient chaque signal recu, envoye ou filtre, avec la raison.

## Limites connues

- Le Pine Script calcule les indicateurs cote TradingView ; `src/analysis.js` peut recalculer/confirmer si
  tu lui fournis des bougies via l'outil MCP `analyze_candles`, mais ce n'est pas branche sur une source de
  donnees temps reel par defaut (a faire si tu veux une double verification independante de TradingView).
- Pas de gestion multi-utilisateur, pas de base de donnees — `rules.json` et `logs/signals.log` sont les
  seules sources d'etat.
