# Chordarr image. Conventions of the *arr images: /config for data, /music and
# /downloads for media, PUID/PGID to run as your user, port 8585.
FROM python:3.13-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY chordarr ./chordarr
RUN pip install --no-cache-dir --prefix=/install ".[streamrip]"

FROM python:3.13-slim
LABEL org.opencontainers.image.source="https://github.com/gbuyssens/chordarr" \
      org.opencontainers.image.description="Music collection manager for the *arr stack" \
      org.opencontainers.image.licenses="MIT"
RUN apt-get update && apt-get install -y --no-install-recommends gosu tini && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 chordarr && useradd -u 1000 -g 1000 -d /config -s /usr/sbin/nologin chordarr
COPY --from=build /install /usr/local
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /config /music /downloads
ENV CHORDARR_DATA_DIR=/config CHORDARR_ROOT_FOLDER=/music CHORDARR_PORT=8585 PUID=1000 PGID=1000 TZ=UTC
VOLUME ["/config"]
EXPOSE 8585
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8585/api/v1/system/status').status==200 else 1)" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python", "-m", "chordarr"]
