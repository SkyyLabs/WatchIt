#!/usr/bin/env bash
set -euo pipefail


echo "==> Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
echo "Homebrew not found. Install from https://brew.sh and re-run."
exit 1
fi


echo "==> Installing macOS packages via Brewfile..."
brew bundle --file=Brewfile


echo "==> Creating Python 3.11 virtualenv (.venv)"
if ! command -v python3.11 >/dev/null 2>&1; then
echo "python@3.11 not found on PATH. Ensure Homebrew's python@3.11 is linked."
echo 'You may need: echo 'export PATH="/opt/homebrew/opt/python@3.11/bin:$PATH"' >> ~/.zshrc'
exit 1
fi


python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip wheel setuptools


echo "==> Installing Python requirements..."
pip install -r requirements.txt


echo "==> Starting Ollama service (if not already running)..."
if ! pgrep -x "ollama" >/dev/null 2>&1; then
ollama serve >/tmp/ollama.log 2>&1 &
sleep 2
fi


echo "==> Pulling default model for judge (llama3.1)..."
ollama pull llama3.1


echo "==> All set!"
echo "- Activate your venv with: source .venv/bin/activate"
echo "- Run API (example): make run-api"
echo "- Run dashboard (example): make run-dashboard"
