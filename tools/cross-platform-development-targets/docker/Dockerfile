FROM python:3.12-slim-trixie

ARG BOXTEAM_TARGET_UID=1000
ARG BOXTEAM_TARGET_GID=1000

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        chromium \
        curl \
        git \
        iproute2 \
        lsof \
        openssh-server \
        procps \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libatspi2.0-0 \
        libcairo2 \
        libcups2 \
        libdbus-1-3 \
        libgbm1 \
        libglib2.0-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxcb1 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/npm/node_modules/node-gyp/bin/node-gyp.js /usr/local/bin/node-gyp
COPY --from=oven/bun:1.3.11 /usr/local/bin/bun /usr/local/bin/bun

RUN groupadd --gid "${BOXTEAM_TARGET_GID}" boxteam \
    && useradd --uid "${BOXTEAM_TARGET_UID}" --gid "${BOXTEAM_TARGET_GID}" \
        --create-home --home-dir /home/boxteam --shell /bin/bash boxteam \
    && passwd -d boxteam \
    && mkdir -p /run/sshd /opt/boxteam-dev/repository /opt/boxteam-dev/artifacts /opt/boxteam-dev/ssh

COPY entrypoint.sh /usr/local/bin/boxteam-development-target-entrypoint
RUN chmod 755 /usr/local/bin/boxteam-development-target-entrypoint \
    && printf '%s\n' \
        '#!/bin/sh' \
        'set -eu' \
        'repository=/opt/boxteam-dev/repository' \
        'manifest="$repository/out/development-runtime/runtime-manifest.json"' \
        'test -f "$manifest" || { echo "boxteam: 开发 runtime manifest 尚未生成，请先启动 development profile" >&2; exit 1; }' \
        'export BOXTEAM_RUNTIME_MANIFEST="$manifest"' \
        'export BOXTEAM_HOME="${BOXTEAM_HOME:-/home/boxteam/.boxteams-dev}"' \
        'exec node "$repository/packages/launcher/bin/boxteam.mjs" "$@"' \
        > /usr/local/bin/boxteam \
    && chmod 755 /usr/local/bin/boxteam

WORKDIR /opt/boxteam-dev/repository
EXPOSE 22 8011 8013 8016
ENTRYPOINT ["/usr/local/bin/boxteam-development-target-entrypoint"]
