.PHONY: setup seed samples run eval eval-publish eval-live test lint report api dash clean

PY := .venv/Scripts/python.exe
ifeq ($(OS),)
PY := .venv/bin/python
endif

setup:            ## install dependencies into .venv
	python -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements-dev.txt

samples:          ## regenerate the synthetic emails and quote PDFs
	$(PY) seed/make_samples.py

seed:             ## create master data in a live ERPNext (idempotent)
	$(PY) seed/seed_erpnext.py

run:              ## run one request end to end against the fake ERP
	$(PY) -m procureflow.cli run seed/samples/req-happy.txt

eval:             ## run all scenarios, diffed against eval/baseline.json
	$(PY) eval/run_eval.py --tag scratch --check-baseline

eval-publish:     ## record a new, write-once result: make eval-publish TAG=v4
	$(PY) eval/run_eval.py --tag $(TAG) --check-baseline

eval-live:        ## run all scenarios against a live ERPNext
	$(PY) eval/run_eval.py --tag live --live

test:             ## offline test suite
	$(PY) -m pytest tests -q

lint:
	$(PY) -m ruff check procureflow eval seed tests

report:
	$(PY) -m procureflow.cli report

api:
	$(PY) -m uvicorn procureflow.api:app --reload --port 8000

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__
