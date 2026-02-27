ARG BUILD_FROM
FROM $BUILD_FROM

# ── system deps ──────────────────────────────────────────────────────────────
RUN apk add --no-cache \
        python3 \
        py3-pip \
        bash \
        curl \
        jq

# ── Python deps ───────────────────────────────────────────────────────────────
# Build context is the add-on directory (ha_ai_operator/), so paths are
# relative to that directory.
COPY app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

# ── application files ─────────────────────────────────────────────────────────
COPY run.sh /run.sh
COPY app/ /app/

RUN chmod +x /run.sh

WORKDIR /app

CMD ["/run.sh"]
