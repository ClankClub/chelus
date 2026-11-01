# Chelus test bench: a plain Linux box for trying models and running simulations.
# The source is mounted read-only at /app (see compose.yaml), so nothing here is copied in.
FROM python:3.14-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git procps ripgrep \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 chelus \
 && mkdir /state /out && chown chelus:chelus /state /out

USER chelus
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
ENTRYPOINT ["sh", "/app/bench/entry.sh"]
CMD ["help"]
