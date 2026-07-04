#!/usr/bin/env bash
set -euo pipefail

# One-shot bootstrap for a fresh macOS machine.
# Installs system packages (Brewfile), then hands off to `make setup` for the
# venv, Python deps, and Ollama — so those steps live in exactly one place
# (the Makefile) instead of being duplicated here.

echo "==> Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew not found. Install from https://brew.sh and re-run."
  exit 1
fi

echo "==> Installing system packages via Brewfile..."
brew bundle --file=Brewfile

if ! command -v python3.11 >/dev/null 2>&1; then
  echo "python3.11 not on PATH after brew install. Link Homebrew's python@3.11, e.g.:"
  echo '  echo '\''export PATH="/opt/homebrew/opt/python@3.11/bin:$PATH"'\'' >> ~/.zshrc && exec zsh'
  exit 1
fi

echo "==> Running project setup (venv, Python deps, Ollama, model)..."
make setup

echo "==> Done."
echo "- Activate venv:  source .venv/bin/activate"
echo "- Run API:        make run-api"
echo "- Run dashboard:  make run-dashboard"
