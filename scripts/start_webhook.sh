#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

if [ ! -f ".env" ]; then
  echo "[ERREUR] .env manquant. Copie .env.example en .env et remplis-le d'abord."
  exit 1
fi

npm install
npm run webhook
