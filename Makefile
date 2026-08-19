PYTHON ?= python3

.PHONY: doctor continuity-doctor dependencies-check dev-preflight init layout require-init maintenance-check validate validate-runtime code-search-eval code-search-eval-runtime install install-opencode-ssh opencode-ssh opencode-recreate docker-build docker-up docker-down docker-smoke opencode-ssh-build opencode-ssh-up opencode-ssh-down opencode-ssh-shell opencode-web-password opencode-runtime-check runtime-config embedding-benchmark reranker-benchmark mcp-local mcp-docker mcp-auto index index-local index-vector index-vector-local burp-status burp-tools burp-validate backup-portable backup-full backup-verify backup-inspect restore test clean package

BACKUP_DIR ?= ../awoki-backups
BACKUP ?=
BACKUP_INCLUDE_OPENCODE_STATE ?= 0
BACKUP_INCLUDE_SECRETS ?= 0
BACKUP_ALLOW_LIVE ?= 0
BACKUP_STOP_CONTAINERS ?= 0
RESTORE_FORCE ?= 0
RESTORE_STOP_CONTAINERS ?= 0
RESTORE_REINDEX ?= auto
EMBEDDING_BENCHMARK_ARGS ?=
RERANKER_BENCHMARK_ARGS ?=
OPENCODE_INSTALL_MODE ?= latest
OPENCODE_SAFE_VERSION ?=

truthy = $(filter 1 true yes on,$(strip $(1)))
backup_common_flags = $(if $(call truthy,$(BACKUP_INCLUDE_OPENCODE_STATE)),--include-opencode-state,) $(if $(call truthy,$(BACKUP_INCLUDE_SECRETS)),--include-secrets,) $(if $(call truthy,$(BACKUP_ALLOW_LIVE)),--allow-live,) $(if $(call truthy,$(BACKUP_STOP_CONTAINERS)),--stop-containers,)
restore_flags = $(if $(call truthy,$(RESTORE_FORCE)),--force,) $(if $(call truthy,$(RESTORE_STOP_CONTAINERS)),--stop-containers,) --reindex $(RESTORE_REINDEX)

doctor:
	.harness/bin/doctor

continuity-doctor:
	.harness/bin/awoki doctor

dependencies-check:
	$(PYTHON) .harness/check_runtime_dependencies.py

dev-preflight:
	.harness/bin/awoki-dev-preflight

init:
	./init-awoki.sh

layout: init

require-init:
	@test -f .harness/state/layout_initialized.json || (echo "Awoki base layout is not initialized. Run: ./init-awoki.sh" >&2; exit 2)

maintenance-check:
	@.harness/bin/awoki-backup lock-check >/dev/null

validate:
	$(PYTHON) .harness/check_runtime_dependencies.py
	$(PYTHON) .harness/validate.py
	$(PYTHON) .harness/run_tests.py
	$(PYTHON) .harness/validate_opencode_plugin.py
	@if $(PYTHON) -c 'import tree_sitter_language_pack' >/dev/null 2>&1; then \
		.harness/bin/code-parser-check; \
	else \
		echo "tree-sitter-language-pack is unavailable locally; Docker image builds enforce the mandatory parser smoke test"; \
	fi
	.harness/bin/code-search-eval-check
	bash -n \
		.harness/bin/mcp-docker \
		.harness/bin/mcp-local \
		.harness/bin/mcp-auto \
		.harness/bin/awoki-runtime-env \
		.harness/bin/awoki-runtime-snapshot \
		.harness/bin/awoki-dev-preflight \
		.harness/bin/mcp-preflight \
		.harness/bin/code-parser-check \
		.harness/bin/code-search-eval-check \
		.harness/bin/code-search-eval-runtime \
		.harness/bin/tmux-check \
		.harness/bin/init-global \
		.harness/bin/init-layout \
		.harness/bin/prepare-opencode-ssh-keys \
		.harness/bin/prepare-opencode-web-auth \
		.harness/bin/opencode-web-password \
		.harness/bin/awoki-opencode \
		.harness/bin/prepare-qdrant-storage \
		init-awoki.sh \
		.harness/bin/wait-qdrant \
		.harness/bin/doctor \
		.harness/bin/awoki \
		.harness/bin/awoki-backup \
		.harness/bin/opencode-ssh-entrypoint \
		.harness/bin/run-opencode-ssh \
		.harness/bin/recreate-opencode-runtime \
		.harness/bin/opencode-runtime-compat-check \
		run-opencode.sh \
		open-lavish.sh
	@if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then docker compose config >/dev/null && docker compose -f docker-compose.opencode.yml config >/dev/null; else echo "docker compose not available; skipping compose config validation"; fi

validate-runtime: validate
	@command -v rg >/dev/null 2>&1 || (echo "runtime validation requires ripgrep (rg)" >&2; exit 2)
	@if [ ! -x /usr/local/bin/awoki-go-semantics ]; then command -v go >/dev/null 2>&1 || (echo "runtime validation requires the prebuilt Go semantics helper or a local Go toolchain fallback" >&2; exit 2); fi
	@$(PYTHON) -c 'import tree_sitter_language_pack' >/dev/null 2>&1 || (echo "runtime validation requires tree-sitter-language-pack" >&2; exit 2)
	.harness/bin/code-parser-check
	.harness/bin/code-search-eval-runtime

code-search-eval:
	.harness/bin/code-search-eval-check

code-search-eval-runtime:
	.harness/bin/code-search-eval-runtime

install: require-init doctor docker-build docker-up docker-smoke
	@echo "Awoki host-OpenCode Docker install completed. Start OpenCode on host with: opencode"

install-opencode-ssh: require-init doctor docker-build opencode-ssh-build opencode-ssh-up
	@echo "Awoki OpenCode-over-SSH install completed."
	@echo "SSH with: ssh -i .ssh-container/id_ed25519 -p $${AWOKI_OPENCODE_SSH_PORT:-2222} op@127.0.0.1"
	@echo "OpenCode Web (default): http://127.0.0.1:$${AWOKI_OPENCODE_WEB_PORT:-4096}"
	@echo "Show Web password explicitly with: make opencode-web-password"
	@echo "Recommended inside SSH: cd /awoki && tmux new -A -s awoki"
	@echo "Then run: awoki-opencode"

opencode-ssh: require-init
	./run-opencode.sh

opencode-recreate:
	.harness/bin/recreate-opencode-runtime $(OPENCODE_RECREATE_ARGS)


docker-build:
	docker compose build awoki-mcp

docker-up: maintenance-check
	.harness/bin/prepare-qdrant-storage docker-compose.yml
	docker compose up -d qdrant
	.harness/bin/wait-qdrant


docker-smoke: docker-up
	docker compose run --rm -e AWOKI_RERANK_ENABLED=0 awoki-mcp .harness/bin/code-parser-check
	docker compose run --rm -e AWOKI_RERANK_ENABLED=0 awoki-mcp python -c "import sys; sys.path.insert(0, '.harness'); from harness_core import harness_status; import rag_backend; print(harness_status()); print(rag_backend.embedding_profile())"

docker-down:
	docker compose down
	docker compose -f docker-compose.opencode.yml down 2>/dev/null || true

opencode-ssh-build:
	@case "$(OPENCODE_INSTALL_MODE)" in latest|safe) ;; *) echo "OPENCODE_INSTALL_MODE must be latest or safe" >&2; exit 2;; esac
	@if [ "$(OPENCODE_INSTALL_MODE)" = safe ] && [ -z "$(OPENCODE_SAFE_VERSION)" ]; then echo "safe mode requires OPENCODE_SAFE_VERSION=<exact version>" >&2; exit 2; fi
	AWOKI_OPENCODE_INSTALL_MODE="$(OPENCODE_INSTALL_MODE)" AWOKI_OPENCODE_SAFE_VERSION="$(OPENCODE_SAFE_VERSION)" AWOKI_OPENCODE_RESOLVE_TOKEN="$$(date -u +%Y%m%dT%H%M%S)-$$$$" docker compose -f docker-compose.opencode.yml build awoki-opencode-ssh

opencode-ssh-up: require-init
	.harness/bin/run-opencode-ssh

opencode-ssh-down:
	docker compose -f docker-compose.opencode.yml down

opencode-ssh-shell:
	ssh -i .ssh-container/id_ed25519 -p $${AWOKI_OPENCODE_SSH_PORT:-2222} op@127.0.0.1

opencode-web-password:
	@.harness/bin/opencode-web-password

opencode-runtime-check:
	@docker compose -f docker-compose.opencode.yml exec -T -u root awoki-opencode-ssh \
		/awoki/.harness/bin/awoki-runtime-snapshot >/dev/null
	docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh \
		/awoki/.harness/bin/opencode-runtime-compat-check
	@docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh bash -lc '\
		case "$${AWOKI_OPENCODE_WEB_ENABLED:-1}" in 1|true|TRUE|yes|YES|on|ON) \
		  /awoki/.harness/bin/opencode-web-health --url "http://127.0.0.1:$${AWOKI_OPENCODE_WEB_PORT:-4096}" --username "$${AWOKI_OPENCODE_WEB_USERNAME:-opencode}" --password-file /run/awoki/opencode-web-password ;; \
		esac'
	docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh \
		/awoki/.harness/bin/awoki-runtime-env --profile qdrant -- bash -lc 'set -euo pipefail; \
			test "$${AWOKI_ROOT:-}" = /awoki; \
			test "$${AWOKI_MODE:-}" = container-opencode; \
			test "$${AWOKI_QDRANT_URL:-}" = http://qdrant:6333; \
			HOME=/home/op /awoki/.harness/bin/mcp-preflight; \
			HOME=/home/op /awoki/.harness/bin/code-parser-check >/dev/null; \
			HOME=/home/op TERM=xterm-256color /awoki/.harness/bin/tmux-check'

runtime-config:
	@if [ -r /run/awoki/runtime.env ] && [ -x .harness/bin/awoki-runtime-env ]; then \
		.harness/bin/awoki-runtime-env --print-config; \
	elif command -v docker >/dev/null 2>&1 && docker compose -f docker-compose.opencode.yml ps -q awoki-opencode-ssh 2>/dev/null | grep -q .; then \
		docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh /awoki/.harness/bin/awoki-runtime-env --print-config; \
	else \
		echo "Awoki SSH runtime is unavailable. Start it with: make opencode-ssh-up" >&2; exit 2; \
	fi

embedding-benchmark:
	@if [ -r /run/awoki/runtime.env ] && [ -x .harness/bin/awoki-runtime-env ]; then \
		.harness/bin/awoki-runtime-env --profile retrieval -- .harness/bin/embedding-benchmark $(EMBEDDING_BENCHMARK_ARGS); \
	elif command -v docker >/dev/null 2>&1 && docker compose -f docker-compose.opencode.yml ps -q awoki-opencode-ssh 2>/dev/null | grep -q .; then \
		docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh /awoki/.harness/bin/awoki-runtime-env --profile retrieval -- /awoki/.harness/bin/embedding-benchmark $(EMBEDDING_BENCHMARK_ARGS); \
	else \
		echo "Awoki SSH runtime is unavailable. Start it with: make opencode-ssh-up" >&2; exit 2; \
	fi

reranker-benchmark:
	@if [ -r /run/awoki/runtime.env ] && [ -x .harness/bin/awoki-runtime-env ]; then \
		.harness/bin/awoki-runtime-env --profile retrieval -- .harness/bin/reranker-benchmark $(RERANKER_BENCHMARK_ARGS); \
	elif command -v docker >/dev/null 2>&1 && docker compose -f docker-compose.opencode.yml ps -q awoki-opencode-ssh 2>/dev/null | grep -q .; then \
		docker compose -f docker-compose.opencode.yml exec -T -u op awoki-opencode-ssh /awoki/.harness/bin/awoki-runtime-env --profile retrieval -- /awoki/.harness/bin/reranker-benchmark $(RERANKER_BENCHMARK_ARGS); \
	else \
		echo "Awoki SSH runtime is unavailable. Start it with: make opencode-ssh-up" >&2; exit 2; \
	fi

mcp-docker:
	.harness/bin/mcp-docker

mcp-local:
	.harness/bin/mcp-local

mcp-auto:
	.harness/bin/mcp-auto

# Docker-first indexing. Use these unless you intentionally installed local Python deps.
index: maintenance-check
	docker compose run --rm awoki-mcp python -c "import sys; sys.path.insert(0, '.harness'); from harness_core import index_all; print(index_all(include_artifacts=True, include_code=False, include_qdrant=False))"

index-vector: docker-up
	docker compose run --rm awoki-mcp python -c "import sys; sys.path.insert(0, '.harness'); from harness_core import index_all; print(index_all(include_artifacts=True, include_code=False, include_qdrant=True))"

index-local: maintenance-check
	$(PYTHON) -c "import sys; sys.path.insert(0, '.harness'); from harness_core import index_all; print(index_all(include_artifacts=True, include_code=False, include_qdrant=False))"

index-vector-local: maintenance-check
	$(PYTHON) -c "import sys; sys.path.insert(0, '.harness'); from harness_core import index_all; print(index_all(include_artifacts=True, include_code=False, include_qdrant=True))"


# Runtime-data migration. Backups are written outside the repository by default.
# Both modes exclude .env, SSH client keys, and OpenCode state unless explicitly requested.
backup-portable: require-init
	.harness/bin/awoki-backup create --mode portable --output-dir "$(BACKUP_DIR)" $(backup_common_flags)

backup-full: require-init
	.harness/bin/awoki-backup create --mode full --output-dir "$(BACKUP_DIR)" $(backup_common_flags)

backup-verify:
	@test -n "$(BACKUP)" || (echo "Set BACKUP=/path/to/awoki-*.tar.gz" >&2; exit 2)
	.harness/bin/awoki-backup verify "$(BACKUP)"

backup-inspect:
	@test -n "$(BACKUP)" || (echo "Set BACKUP=/path/to/awoki-*.tar.gz" >&2; exit 2)
	.harness/bin/awoki-backup inspect "$(BACKUP)"

restore:
	@test -n "$(BACKUP)" || (echo "Set BACKUP=/path/to/awoki-*.tar.gz" >&2; exit 2)
	.harness/bin/awoki-backup restore "$(BACKUP)" $(restore_flags)

burp-status: maintenance-check
	docker compose run --rm awoki-mcp python .harness/integrations/burp/awoki_burp.py status

burp-tools: maintenance-check
	docker compose run --rm awoki-mcp python .harness/integrations/burp/awoki_burp.py tools --save

burp-validate: maintenance-check
	docker compose run --rm awoki-mcp python .harness/integrations/burp/awoki_burp.py validate

test:
	$(PYTHON) -m unittest discover -s .harness/tests -p 'test_*.py'

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +

package: validate
	git bundle create ../awoki.git.bundle --all
	git archive --format=tar.gz --prefix=awoki/ --output=../awoki.tar.gz HEAD
