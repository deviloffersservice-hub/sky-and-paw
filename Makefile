# Sky & Paw: one entry point for setup, run, test, build.
#   make setup     .venv (Python >= 3.11, prefers python3.11) + npm ci + frontend build (idempotent)
#   make run       backend on :7860, serving frontend/dist at / when built
#   make test      ruff + pytest + frontend typecheck
#   make build     build the frontend into frontend/dist (honours VITE_BASE)
#   make smoke     build, start the backend, Playwright smoke (healthz, title, /ws)
#   make setup-ml  optional ML stack (ultralytics 8.3 + torch CPU), needed from M3
#   make docker    build the optional Docker image
#   make lock      regenerate backend/requirements*.txt from backend/pyproject.toml
#   make scene     create/reuse the Cyberwave scene, run MuJoCo (120 s), save frames (billable)
#   make scene-dry print the scene plan (GET requests only, no simulation)

SHELL := /bin/bash
.DEFAULT_GOAL := help

PYTHON ?= $(shell command -v python3.11 2>/dev/null || command -v python3 2>/dev/null)
VENV := .venv
VPY := $(VENV)/bin/python
PIP := $(VPY) -m pip --disable-pip-version-check
HOST ?= 0.0.0.0
PORT ?= 7860
DATA_DIR ?= $(CURDIR)/data
IMAGE ?= skypaw:dev
GIT_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo unknown)
export DATA_DIR GIT_SHA

PY_STAMP := $(VENV)/.skypaw-setup
ML_STAMP := $(VENV)/.skypaw-setup-ml
NODE_STAMP := frontend/node_modules/.skypaw-setup
DIST := frontend/dist/index.html
FRONTEND_SRC := $(wildcard frontend/src/*) frontend/index.html frontend/vite.config.ts frontend/tsconfig.json

.PHONY: help setup setup-ml run test lint pytest typecheck build smoke docker lock clean scene scene-dry glb FORCE

help:
	@sed -n '2,11p' $(firstword $(MAKEFILE_LIST)) | sed 's/^# *//'

setup: $(PY_STAMP) $(NODE_STAMP) $(DIST)

# Built by setup/run when missing, when sources change, or when the base it was built with
# (frontend/dist/.vite-base, written by vite.config.ts) differs from the requested VITE_BASE
# (default /). So after `VITE_BASE=/sky-and-paw/ make build`, `make run` rebuilds for /.
WANT_BASE := $(if $(filter-out /,$(strip $(VITE_BASE))),$(strip $(VITE_BASE)),/)
BUILT_BASE := $(strip $(shell cat frontend/dist/.vite-base 2>/dev/null))
ifneq ($(BUILT_BASE),$(WANT_BASE))
$(DIST): FORCE
endif
$(DIST): $(NODE_STAMP) $(FRONTEND_SRC)
	cd frontend && npm run build

FORCE:

$(PY_STAMP): backend/requirements-dev.txt backend/pyproject.toml
	@test -n "$(PYTHON)" || { echo "python3 not found" >&2; exit 1; }
	@$(PYTHON) -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
		|| { echo "Python >= 3.11 required (found $$($(PYTHON) --version))" >&2; exit 1; }
	@test -x $(VPY) || $(PYTHON) -m venv $(VENV)
	$(PIP) install -q -r backend/requirements-dev.txt
	$(PIP) install -q --no-deps -e backend
	@touch $@

$(NODE_STAMP): frontend/package.json frontend/package-lock.json
	cd frontend && npm ci --no-audit --no-fund
	@touch $@

# Full lock installed with --no-deps: keeps opencv-python-headless (ultralytics would
# otherwise pull the GUI opencv-python next to it).
setup-ml: $(ML_STAMP)
$(ML_STAMP): $(PY_STAMP) backend/requirements-ml.txt
	$(PIP) install -q --no-deps -r backend/requirements-ml.txt
	$(VPY) -c "import torch, ultralytics; print('torch', torch.__version__, 'ultralytics', ultralytics.__version__)"
	@touch $@

run: $(PY_STAMP) $(DIST)
	$(VENV)/bin/uvicorn skypaw.app:create_app --factory --host $(HOST) --port $(PORT)

test: lint pytest typecheck

LINT_SCRIPTS := scripts/scene.py scripts/overhead.py

lint: $(PY_STAMP)
	$(VENV)/bin/ruff check backend
	$(VENV)/bin/ruff format --check backend
	$(VENV)/bin/ruff check --config backend/pyproject.toml $(LINT_SCRIPTS)
	$(VENV)/bin/ruff format --check --config backend/pyproject.toml $(LINT_SCRIPTS)

pytest: $(PY_STAMP)
	$(VENV)/bin/pytest backend

typecheck: $(NODE_STAMP)
	cd frontend && npm run --silent typecheck

build: $(NODE_STAMP)
	cd frontend && npm run build

smoke: $(PY_STAMP) build
	scripts/smoke.sh

# M1 scene. SCENE_ARGS adds flags, e.g. SCENE_ARGS="--env skypaw-m1-dev --duration 90".
# `make scene` starts a billable MuJoCo run (default 120 s = 0.02 credits) and always stops it.
SCENE_ARGS ?=
scene: $(PY_STAMP)
	$(VPY) scripts/scene.py --capture $(SCENE_ARGS)

scene-dry: $(PY_STAMP)
	$(VPY) scripts/scene.py --dry-run $(SCENE_ARGS)

# Rebuild backend/skypaw/scene/rubble.glb from site.yaml (offline).
glb: $(PY_STAMP)
	$(VPY) -m skypaw.scene.build_glb

docker:
	docker build --build-arg GIT_SHA=$(GIT_SHA) -t $(IMAGE) .

lock:
	@tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	$(PYTHON) -m venv "$$tmp" && "$$tmp/bin/pip" install -q pip-tools==7.4.1 && \
	cd backend && export CUSTOM_COMPILE_COMMAND="make lock" && \
	"$$tmp/bin/pip-compile" -q --strip-extras --no-emit-index-url -o requirements.txt pyproject.toml && \
	"$$tmp/bin/pip-compile" -q --strip-extras --no-emit-index-url --extra dev -o requirements-dev.txt pyproject.toml && \
	"$$tmp/bin/pip-compile" -q --strip-extras --emit-index-url --unsafe-package opencv-python \
		-o requirements-ml.txt requirements-ml.in && \
	sed -i.bak -e '/^numpy==1\.26\.4 ; python_version < "3\.13"$$/a\' \
		-e 'numpy==2.1.3 ; python_version >= "3.13"' \
		requirements.txt requirements-dev.txt requirements-ml.txt && \
	sed -i.bak -e '/^--extra-index-url https:\/\/download\.pytorch\.org\/whl\/cpu$$/i\' \
		-e '$(ML_INDEX_NOTE)' requirements-ml.txt && \
	rm -f ./*.bak
# pip-compile resolves for the running interpreter (3.11) and drops the numpy pin whose marker
# does not match it; the first sed re-adds the Python 3.13 line from pyproject.toml. The second
# re-adds the note on the CPU index (pip-compile does not copy comments from the .in file).
# `sed -i.bak` + `rm` works with both GNU and BSD sed.
ML_INDEX_NOTE := \# torch/torchvision are pinned to exact +cpu builds, which only this index serves.

clean:
	rm -rf frontend/dist frontend/test-results backend/.pytest_cache backend/.ruff_cache .ruff_cache \
		backend/*.egg-info
	find backend -name __pycache__ -type d -prune -exec rm -rf {} +
