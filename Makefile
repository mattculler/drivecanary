# Development targets, plus install/update for the VM (deploy/README.md).
export DRIVECANARY_CONFIG ?= dev/config.toml
UV ?= uv

.PHONY: not-root sync dev-config dev-db migrate test lint typecheck check config-example install update

# Development targets run uv as you, in your checkout. Under sudo, or in the installed checkout, uv would rewrite
# /opt/drivecanary/.venv with root-owned files and the next `drivecanary update` could not sync it.
not-root:
	@[ "$$(id -u)" -ne 0 ] || { echo "not as root: these targets are for a dev checkout; on the VM use 'drivecanary update'"; exit 1; }

sync: not-root
	$(UV) sync

# A dev config pointing everything at ./dev (gitignored): database, ssh material, backups.
dev-config: not-root
	@mkdir -p dev
	@[ -f dev/config.toml ] || { $(UV) run drivecanary config example --state-dir "$$(pwd)/dev" > dev/config.toml; echo "wrote dev/config.toml"; }

# Create/upgrade the dev database schema.
dev-db migrate: not-root dev-config
	$(UV) run drivecanary db migrate

test: not-root
	$(UV) run pytest

lint: not-root
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

typecheck: not-root
	$(UV) run mypy

check: lint typecheck test

# Regenerate the documented example config from the settings model.
config-example: not-root
	$(UV) run drivecanary config example > deploy/config.example.toml

# First install on the VM (root): users, dirs, units, venv, migrate, enable timers. See deploy/README.md.
install:
	sudo deploy/install.sh

# Pull, sync, migrate, restart the web UI; the timers pick up the new code on their next run.
update:
	sudo deploy/update.sh
