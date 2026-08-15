SHELL := /bin/bash
CORPUS ?= a
COMPOSE := docker compose
VOL := codekg_corpus

# Hermetic python for host-side helpers - don't require pyyaml in the system
# interpreter. uv pulls it into a throwaway env.
PY := $(shell command -v uv >/dev/null 2>&1 && echo "uv run --quiet --with pyyaml python3" || echo python3)

.PHONY: help up down clean corpus sync extract load link demo stats shell logs

help:
	@echo "Vertical slice, in order:"
	@echo "  make up                    start Neo4j (http://localhost:7474)"
	@echo "  make corpus CORPUS=a       clone + pin corpus A to corpus/src/"
	@echo "  make sync                  copy corpus into the docker volume"
	@echo "  make extract CORPUS=a      run graphify over every repo in the corpus"
	@echo "  make extract-codegraph CORPUS=a   second extractor (agreement matrix)"
	@echo "  make extract-gitnexus CORPUS=a    third extractor (majority vote)"
	@echo "  make goscan CORPUS=b       Go AST scan (required for Go corpora)"
	@echo "  make inspect REPO=<id>     dump one artifact's real shape (do this first)"
	@echo "  make load CORPUS=a         normalize + load into Neo4j"
	@echo "  make enrich CORPUS=a       add source-derived imports the artifact omits"
	@echo "  make link                  run the cross-repo resolution pass"
	@echo "  make demo SYM=<symbol-id>  blast radius, joined vs single-repo"
	@echo "  make score CORPUS=a        precision/recall vs source-derived truth"
	@echo "  make gds                   org-level Leiden / betweenness / articulation points"
	@echo ""
	@echo ""
	@echo "  make nes                   start Neo4j Enterprise Studio (Query + Bloom)"
	@echo "  make nes-down              stop Studio, leave Neo4j running"
	@echo ""
	@echo "  make stats / logs / shell / down / clean"

up:
	$(COMPOSE) up -d neo4j
	@echo "waiting for neo4j..."
	@until [ "$$($(COMPOSE) ps neo4j --format '{{.Health}}' 2>/dev/null)" = "healthy" ]; do \
		printf "."; sleep 3; \
	done; echo " ready"
	$(COMPOSE) run --rm loader init

nes:
	@test -f "$${NES_LICENSE_PATH:-../nes-docker/internal.license}" || { \
		echo "license not found at $${NES_LICENSE_PATH:-../nes-docker/internal.license}"; \
		echo "set NES_LICENSE_PATH in .env to point at your NES license"; exit 1; }
	$(COMPOSE) --profile nes up -d
	@echo "waiting for Enterprise Studio..."
	@until [ "$$($(COMPOSE) ps enterprise-studio --format '{{.Health}}' 2>/dev/null)" = "healthy" ]; do \
		printf "."; sleep 5; \
	done; echo " ready"
	@echo ""
	@echo "  Studio:     http://localhost:$${NES_PORT:-8080}"
	@echo "  Sign in:    neo4j / $${NEO4J_PASSWORD:-codekgcodekg}"
	@echo "  Deployment: codekg   ->   database: neo4j"

nes-down:
	$(COMPOSE) --profile nes stop enterprise-studio

down:
	$(COMPOSE) --profile nes down

clean:
	$(COMPOSE) down -v
	rm -rf corpus/src

corpus:
	@chmod +x corpus/fetch.sh
	./corpus/fetch.sh $(CORPUS) $(if $(FULL),--full,)

# Corpus lives in a named volume rather than a bind mount. macOS virtiofs drops
# inotify events, which silently breaks any extractor that watches files -
# relevant the moment CodeGraph joins the harness.
sync:
	@# Recreate the volume through compose (not `docker volume create`) so it
	@# carries compose's labels - otherwise every later compose command warns
	@# that the volume "already exists but was not created by Docker Compose".
	@# Dropping it first is free: the copy below repopulates it wholesale.
	@docker volume rm -f $(VOL) >/dev/null 2>&1 || true
	@$(COMPOSE) run --rm -T --entrypoint sh graphify -c 'true' </dev/null >/dev/null 2>&1 || true
	@docker run --rm \
		-v $(VOL):/dest \
		-v "$(PWD)/corpus/src:/src:ro" \
		alpine:3 sh -c 'rm -rf /dest/* && cp -a /src/. /dest/ && echo "synced: $$(du -sh /dest | cut -f1)"'

extract:
	@$(COMPOSE) build graphify
	@$(PY) corpus/_list_repos.py corpus/corpus.yaml $(CORPUS) $(if $(FULL),full,core) \
	| while read -r id subpath; do \
		$(COMPOSE) run --rm -T graphify $(CORPUS) "$$id" "$$subpath" </dev/null || \
			echo "!! extraction failed for $$id - continuing"; \
	done

inspect:
	@test -n "$(REPO)" || (echo "usage: make inspect REPO=<repo-id> [CORPUS=a]"; exit 2)
	$(COMPOSE) run --rm loader inspect graphify $(CORPUS) $(REPO)

load:
	$(COMPOSE) run --rm loader load $(CORPUS) --extractor graphify --replace

extract-codegraph:
	@$(COMPOSE) build codegraph
	@$(PY) corpus/_list_repos.py corpus/corpus.yaml $(CORPUS) $(if $(FULL),full,core) \
	| while read -r id subpath; do \
		$(COMPOSE) run --rm -T codegraph $(CORPUS) "$$id" "$$subpath" </dev/null || \
			echo "!! codegraph failed for $$id - continuing"; \
	done

extract-gitnexus:
	@$(COMPOSE) build gitnexus
	@$(PY) corpus/_list_repos.py corpus/corpus.yaml $(CORPUS) $(if $(FULL),full,core) \
	| while read -r id subpath; do \
		$(COMPOSE) run --rm -T gitnexus $(CORPUS) "$$id" "$$subpath" </dev/null || \
			echo "!! gitnexus failed for $$id - continuing"; \
	done

goscan:
	@$(COMPOSE) build goscan
	@$(PY) corpus/_list_repos.py corpus/corpus.yaml $(CORPUS) $(if $(FULL),full,core) \
	| while read -r id subpath; do \
		echo "==> goscan $$id"; \
		$(COMPOSE) run --rm -T --entrypoint sh goscan -c \
			"mkdir -p /out/goscan/$(CORPUS) && /usr/local/bin/goscan /corpus/$(CORPUS)/$$id > /out/goscan/$(CORPUS)/$$id.json" </dev/null; \
	done

enrich:
	$(COMPOSE) run --rm -T loader enrich $(CORPUS) </dev/null

link:
	$(COMPOSE) run --rm -T loader link </dev/null

# The whole point, in two queries. Q1 is the joined graph; Q2 is the same
# question with the corpus confined to one repo, which is what any single tool
# can tell you. Show them back to back.
demo:
	@test -n "$(SYM)" || (echo 'usage: make demo SYM="sym:repo:...#Class.method"'; exit 2)
	@# SYM must stay quoted - symbol ids contain '#' and ':' and will be
	@# mangled by the shell otherwise.
	@echo "=== Q1: cross-repo blast radius (joined graph) ==="
	@$(COMPOSE) run --rm -T loader query q1 --param "changed=$(SYM)" </dev/null
	@echo ""
	@echo "=== Q2: same question, single-repo scope (what one tool sees) ==="
	@$(COMPOSE) run --rm -T loader query q2 --param "changed=$(SYM)" </dev/null
	@echo ""
	@echo "=== Q3: evidence - shortest concrete path per impacted repo ==="
	@$(COMPOSE) run --rm -T loader query q3 --param "changed=$(SYM)" </dev/null

gds:
	@$(COMPOSE) run --rm -T loader gds </dev/null

score:
	$(COMPOSE) run --rm -T loader score $(CORPUS) --json /artifacts/score-$(CORPUS).json </dev/null

stats:
	$(COMPOSE) run --rm -T loader stats </dev/null

logs:
	$(COMPOSE) logs -f neo4j

shell:
	$(COMPOSE) exec neo4j cypher-shell -u neo4j -p $${NEO4J_PASSWORD:-codekgcodekg}
