# Pinned to a digest: a tag can be re-pointed, a digest cannot (checked 2026-10-14).
# Dependabot (docker ecosystem) proposes new digests; the tag stays for readability.
FROM python:3.15-slim@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # metrics.db, budget.json, prices.json: the app user has no home, so keep them in the logs volume.
    TRIODITIS_HOME=/app/logs/trioditis \
    # Where images before 0.9.1 kept them. With both set, `serve` moves the files from here into
    # TRIODITIS_HOME when it starts (a link stays at each old path); on a new volume it does nothing.
    HLIDSKJALV_HOME=/app/logs/hlidskjalv

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
# --require-hashes: every package must match the lockfile hash, or the build fails.
RUN pip install --require-hashes --no-deps -r requirements.txt

COPY trioditis ./trioditis
COPY trioditis_metrics ./trioditis_metrics
RUN mkdir -p /app/logs && chown app /app/logs

USER 10001
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=2)"

# Binding 0.0.0.0 inside the container requires TRIODITIS_API_KEY (enforced at start).
CMD ["python", "-m", "trioditis", "--config", "/config/config.yaml", "serve", "--host", "0.0.0.0", "--port", "8787"]
