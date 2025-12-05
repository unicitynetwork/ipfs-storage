# IPFS Storage Service Makefile
# Easy commands for building, running, and managing the service

.PHONY: build run stop logs shell info health clean dev

# Configuration (override via CLI: make run DOMAIN=example.com SSL_CERT=/path/to/cert.pem)
IMAGE_NAME ?= ipfs-storage
CONTAINER_NAME ?= ipfs-storage
VOLUME_NAME ?= ipfs-data
DOMAIN ?= localhost
SSL_CERT ?=
SSL_KEY ?=
PIN_LIST ?=

# Port configuration (override to avoid conflicts)
PORT_SWARM ?= 4001
PORT_WSS ?= 4003
PORT_HTTPS ?= 443
PORT_HTTP ?= 9080

# Nostr pinner configuration (integrated in container, Unicity relays)
NOSTR_RELAYS ?= wss://nostr-relay.testnet.unicity.network,ws://unicity-nostr-relay-20250927-alb-1919039002.me-central-1.elb.amazonaws.com:8080
PIN_KIND ?= 30078
LOG_LEVEL ?= INFO

# Default target
.DEFAULT_GOAL := help

help: ## Show this help
	@echo "IPFS Storage Service"
	@echo ""
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'

build: ## Build Docker image
	docker build -t $(IMAGE_NAME):latest .

run: ## Run container with SSL (SSL_CERT, SSL_KEY, DOMAIN, PIN_LIST optional)
	@if [ -z "$(SSL_CERT)" ] || [ -z "$(SSL_KEY)" ]; then \
		echo "ERROR: SSL_CERT and SSL_KEY are required"; \
		echo "Usage: make run SSL_CERT=/path/to/fullchain.pem SSL_KEY=/path/to/privkey.pem DOMAIN=example.com"; \
		echo "       Optional: PIN_LIST=/path/to/cids.txt"; \
		echo "Use 'make dev' for development mode with self-signed certs"; \
		exit 1; \
	fi
	@if [ ! -f "$(SSL_CERT)" ]; then \
		echo "ERROR: SSL certificate not found at $(SSL_CERT)"; \
		exit 1; \
	fi
	@if [ ! -f "$(SSL_KEY)" ]; then \
		echo "ERROR: SSL key not found at $(SSL_KEY)"; \
		exit 1; \
	fi
	@if [ -n "$(PIN_LIST)" ] && [ ! -f "$(PIN_LIST)" ]; then \
		echo "ERROR: Pin list file not found at $(PIN_LIST)"; \
		exit 1; \
	fi
	docker volume inspect $(VOLUME_NAME) >/dev/null 2>&1 || docker volume create $(VOLUME_NAME)
	@PIN_MOUNT=""; \
	if [ -n "$(PIN_LIST)" ] && [ -f "$(PIN_LIST)" ]; then \
		PIN_MOUNT="-v $(PIN_LIST):/data/pin-list.txt:ro"; \
		echo "Pin list will be mounted from $(PIN_LIST)"; \
	fi; \
	HTTPS_PORT=""; \
	if [ -n "$(PORT_HTTPS)" ]; then \
		HTTPS_PORT="-p $(PORT_HTTPS):443"; \
	fi; \
	docker run -d --name $(CONTAINER_NAME) \
		--restart=always \
		-v $(VOLUME_NAME):/data/ipfs \
		-v $(SSL_CERT):/etc/ssl/certs/fullchain.pem:ro \
		-v $(SSL_KEY):/etc/ssl/private/privkey.pem:ro \
		$$PIN_MOUNT \
		-p $(PORT_SWARM):4001/tcp \
		-p $(PORT_SWARM):4001/udp \
		-p $(PORT_WSS):4003 \
		$$HTTPS_PORT \
		-p $(PORT_HTTP):8080 \
		-e DOMAIN=$(DOMAIN) \
		-e NOSTR_RELAYS="$(NOSTR_RELAYS)" \
		-e PIN_KIND=$(PIN_KIND) \
		-e LOG_LEVEL=$(LOG_LEVEL) \
		$(IMAGE_NAME):latest
	@echo ""
	@echo "Container started. Use 'make logs' to view output."
	@echo "Ports: Swarm=$(PORT_SWARM) WSS=$(PORT_WSS) HTTPS=$(PORT_HTTPS) HTTP=$(PORT_HTTP)"

dev: ## Run in development mode (self-signed cert, PIN_LIST optional)
	docker volume inspect $(VOLUME_NAME) >/dev/null 2>&1 || docker volume create $(VOLUME_NAME)
	@PIN_MOUNT=""; \
	if [ -n "$(PIN_LIST)" ] && [ -f "$(PIN_LIST)" ]; then \
		PIN_MOUNT="-v $(PIN_LIST):/data/pin-list.txt:ro"; \
		echo "Pin list will be mounted from $(PIN_LIST)"; \
	fi; \
	HTTPS_PORT=""; \
	if [ -n "$(PORT_HTTPS)" ]; then \
		HTTPS_PORT="-p $(PORT_HTTPS):443"; \
	fi; \
	docker run -d --name $(CONTAINER_NAME) \
		--restart=always \
		-v $(VOLUME_NAME):/data/ipfs \
		$$PIN_MOUNT \
		-p $(PORT_SWARM):4001/tcp \
		-p $(PORT_SWARM):4001/udp \
		-p $(PORT_WSS):4003 \
		$$HTTPS_PORT \
		-p $(PORT_HTTP):8080 \
		-e DOMAIN=localhost \
		-e DEV_MODE=true \
		-e NOSTR_RELAYS="$(NOSTR_RELAYS)" \
		-e PIN_KIND=$(PIN_KIND) \
		-e LOG_LEVEL=$(LOG_LEVEL) \
		$(IMAGE_NAME):latest
	@echo ""
	@echo "Development container started with self-signed certificate."
	@echo "Ports: Swarm=$(PORT_SWARM) WSS=$(PORT_WSS) HTTPS=$(PORT_HTTPS) HTTP=$(PORT_HTTP)"

stop: ## Stop and remove container
	docker stop $(CONTAINER_NAME) 2>/dev/null || true
	docker rm $(CONTAINER_NAME) 2>/dev/null || true

restart: stop ## Restart container (pass SSL_CERT, SSL_KEY, DOMAIN, PIN_LIST, PORT_* as with run)
	@$(MAKE) run SSL_CERT=$(SSL_CERT) SSL_KEY=$(SSL_KEY) DOMAIN=$(DOMAIN) PIN_LIST=$(PIN_LIST) \
		PORT_SWARM=$(PORT_SWARM) PORT_WSS=$(PORT_WSS) PORT_HTTPS=$(PORT_HTTPS) PORT_HTTP=$(PORT_HTTP)

logs: ## View container logs
	docker logs -f $(CONTAINER_NAME)

shell: ## Open shell in container
	docker exec -it $(CONTAINER_NAME) /bin/bash

info: ## Show peer info and connection details
	@echo "========================================"
	@echo "  IPFS Storage Service Info"
	@echo "========================================"
	@docker exec $(CONTAINER_NAME) ipfs --api=/ip4/127.0.0.1/tcp/5001 id 2>/dev/null || echo "Container not running"
	@echo ""
	@echo "Endpoints:"
	@echo "  Swarm TCP:  /ip4/<host>/tcp/4001"
	@echo "  Swarm UDP:  /ip4/<host>/udp/4001/quic-v1"
	@echo "  WebSocket:  ws://<host>:4002"
	@echo "  WSS:        wss://$(DOMAIN):4003"
	@echo "  Gateway:    https://$(DOMAIN):443/ipfs/<CID>"
	@echo "  HTTP:       http://<host>:9080/ipfs/<CID>"
	@echo ""

health: ## Check service health
	@docker exec $(CONTAINER_NAME) /usr/local/bin/healthcheck.sh

pins: ## List all pinned CIDs
	docker exec $(CONTAINER_NAME) ipfs --api=/ip4/127.0.0.1/tcp/5001 pin ls --type=recursive

pin: ## Pin a CID (usage: make pin CID=Qm...)
	@if [ -z "$(CID)" ]; then echo "Usage: make pin CID=<cid>"; exit 1; fi
	docker exec $(CONTAINER_NAME) ipfs --api=/ip4/127.0.0.1/tcp/5001 pin add --progress $(CID)

unpin: ## Unpin a CID (usage: make unpin CID=Qm...)
	@if [ -z "$(CID)" ]; then echo "Usage: make unpin CID=<cid>"; exit 1; fi
	docker exec $(CONTAINER_NAME) ipfs --api=/ip4/127.0.0.1/tcp/5001 pin rm $(CID)

peers: ## List connected peers
	docker exec $(CONTAINER_NAME) ipfs --api=/ip4/127.0.0.1/tcp/5001 swarm peers

gc: ## Run garbage collection
	docker exec $(CONTAINER_NAME) ipfs --api=/ip4/127.0.0.1/tcp/5001 repo gc

clean: stop ## Remove container and volume
	docker volume rm $(VOLUME_NAME) 2>/dev/null || true
	@echo "Container and volume removed"

# Docker Compose commands
compose-up: ## Start with docker-compose (SSL_CERT, SSL_KEY, DOMAIN required)
	@if [ -z "$(SSL_CERT)" ] || [ -z "$(SSL_KEY)" ] || [ -z "$(DOMAIN)" ] || [ "$(DOMAIN)" = "localhost" ]; then \
		echo "ERROR: SSL_CERT, SSL_KEY, and DOMAIN are required for docker-compose"; \
		echo "Usage: make compose-up SSL_CERT=/path/to/fullchain.pem SSL_KEY=/path/to/privkey.pem DOMAIN=example.com"; \
		echo "       Optional: PIN_LIST=/path/to/cids.txt PORT_HTTPS=443 PORT_WSS=4003"; \
		exit 1; \
	fi
	@if [ -n "$(PIN_LIST)" ]; then \
		echo "Pin list will be mounted from $(PIN_LIST)"; \
	fi
	@echo "Ports: Swarm=$(PORT_SWARM) WSS=$(PORT_WSS) HTTPS=$(PORT_HTTPS) HTTP=$(PORT_HTTP)"
	SSL_CERT=$(SSL_CERT) SSL_KEY=$(SSL_KEY) DOMAIN=$(DOMAIN) PIN_LIST=$(PIN_LIST) \
		PORT_SWARM=$(PORT_SWARM) PORT_WSS=$(PORT_WSS) PORT_HTTPS=$(PORT_HTTPS) PORT_HTTP=$(PORT_HTTP) \
		NOSTR_RELAYS="$(NOSTR_RELAYS)" PIN_KIND=$(PIN_KIND) LOG_LEVEL=$(LOG_LEVEL) \
		docker compose up -d

compose-down: ## Stop docker-compose stack
	docker compose down

compose-logs: ## View docker-compose logs
	docker compose logs -f
