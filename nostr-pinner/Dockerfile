# Nostr IPFS Pin Service
# Subscribes to Nostr relays and pins announced CIDs

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Run as non-root user for security
RUN useradd -r -s /bin/false pinner

# Copy application and set permissions
COPY nostr_pinner.py .
RUN chmod 644 nostr_pinner.py

USER pinner

# Health check - verify process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Environment defaults
ENV NOSTR_RELAYS="wss://relay.damus.io,wss://nos.lol,wss://relay.nostr.band" \
    IPFS_API_URL="http://ipfs:5001" \
    PIN_KIND="30078" \
    LOG_LEVEL="INFO" \
    RECONNECT_DELAY="10" \
    PIN_TIMEOUT="300"

# Run the service
CMD ["python", "-u", "nostr_pinner.py"]
