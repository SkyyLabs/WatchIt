.PHONY: setup venv deps run run-api run-agent-worker run-dashboard start-ollama pull-model db-init clean


VENV=.venv
PY=$(VENV)/bin/python
PIP=$(VENV)/bin/pip
PYTHONPATH=apps/api/src:services/agent-worker/src:services/learning-worker/src:packages/core/src


setup: venv deps start-ollama pull-model ## Full local setup


venv:
	python3.11 -m venv $(VENV)
	@echo "Activate with: source $(VENV)/bin/activate"


deps:
	$(PIP) install --upgrade pip wheel setuptools
	$(PIP) install -r requirements.txt


start-ollama:
	pgrep -x "ollama" >/dev/null 2>&1 || (ollama serve >/tmp/ollama.log 2>&1 & sleep 2)


pull-model:
	ollama pull llama3.1


run: run-api


run-api:
	PYTHONPATH=$(PYTHONPATH) $(VENV)/bin/uvicorn watchit_api.main:app --reload --host 127.0.0.1 --port 4849


run-agent-worker:
	PYTHONPATH=$(PYTHONPATH) $(PY) -m watchit_agents.worker


run-dashboard:
	cd apps/dashboard && npm run dev


db-init:
	$(VENV)/bin/alembic upgrade head


clean:
	rm -rf $(VENV)
	find . -name "__pycache__" -type d -exec rm -rf {} +
