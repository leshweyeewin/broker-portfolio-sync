# MooMoo OpenD gateway sidecar (BUILD_SPEC.md §2, §3, §11).
#
# OpenD is a proprietary binary distributed by MooMoo/Futu — it cannot be
# pip-installed or redistributed here. Provide it to the build in ONE of two ways:
#
#   A) Build-arg URL (recommended for CI): pass the Linux OpenD tarball URL:
#        docker build --build-arg OPEND_URL="https://.../OpenD_x.y_Ubuntu.tar.gz" \
#          -t opend-sidecar opend/
#
#   B) Vendored file: drop the tarball into opend/vendor/OpenD.tar.gz and build
#        normally. (opend/vendor/ is gitignored — do not commit the binary.)
#
# The image contains NO credentials; login details are supplied at runtime as
# env vars (see docker-entrypoint.sh).

FROM ubuntu:22.04

ARG OPEND_URL=""

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/opend

# Bring in the OpenD tarball via URL (A) or vendored copy (B), extract, and
# normalize the entrypoint binary to /opt/opend/OpenD.
COPY vendor/ /tmp/vendor/
RUN set -eux; \
    if [ -n "$OPEND_URL" ]; then \
        curl -fsSL "$OPEND_URL" -o /tmp/opend.tar.gz; \
    elif [ -f /tmp/vendor/OpenD.tar.gz ]; then \
        cp /tmp/vendor/OpenD.tar.gz /tmp/opend.tar.gz; \
    else \
        echo "No OpenD binary: pass --build-arg OPEND_URL=... or vendor opend/vendor/OpenD.tar.gz" >&2; \
        exit 1; \
    fi; \
    tar -xzf /tmp/opend.tar.gz --strip-components=1 -C /opt/opend; \
    rm -f /tmp/opend.tar.gz; \
    # OpenD tarballs name the binary "OpenD" or "OpenD.bin"; normalize it.
    if [ ! -f /opt/opend/OpenD ] && [ -f /opt/opend/OpenD.bin ]; then \
        ln -s /opt/opend/OpenD.bin /opt/opend/OpenD; \
    fi; \
    chmod +x /opt/opend/OpenD*

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 11111
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
