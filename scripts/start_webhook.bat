@echo off
REM Lance le serveur webhook (recoit les alertes TradingView, envoie sur Telegram).
REM Doit rester ouvert en permanence. Utilise avec ngrok/cloudflared pour exposer le port publiquement:
REM   ngrok http %WEBHOOK_PORT%
cd /d "%~dp0.."
if not exist ".env" (
  echo [ERREUR] .env manquant. Copie .env.example en .env et remplis-le d'abord.
  pause
  exit /b 1
)
call npm install
call npm run webhook
pause
