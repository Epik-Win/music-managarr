# A forward proxy outside MusicAI's VPN, for its metadata lookups only
# (METADATA_PROXY; see src/metadata_proxy.py). Downloads never use it.
FROM alpine:3.20
RUN apk add --no-cache tinyproxy
COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
EXPOSE 8888
HEALTHCHECK --interval=60s --timeout=5s CMD nc -z 127.0.0.1 8888 || exit 1
CMD ["tinyproxy", "-d", "-c", "/etc/tinyproxy/tinyproxy.conf"]
