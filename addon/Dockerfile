ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python runtime
RUN apk add --no-cache python3 py3-pip

# Working directory
WORKDIR /app

# Install pinned Python deps first (better layer caching)
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Application code
COPY mcp_server.py claude_coach.py ./

# Startup script
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Note: not dropping to non-root because bashio / HA supervisor integration
# requires root for reading /data/options.json. Running as root inside the
# container is the HA addon convention — the container itself is the boundary.

CMD ["/run.sh"]
