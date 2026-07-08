ARG SSH_TEXT_BASE_IMAGE=ssh-text-attach-node:22-bookworm-slim
FROM ${SSH_TEXT_BASE_IMAGE}

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG ALL_PROXY
ARG http_proxy
ARG https_proxy
ARG all_proxy

RUN apt-get update \
  && apt-get install -y --no-install-recommends openssh-server tini \
  && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash demo \
  && passwd -d demo \
  && mkdir -p /run/sshd

WORKDIR /workspace/demo

COPY docker/entrypoint.sh /usr/local/bin/ssh-text-entrypoint
RUN chmod +x /usr/local/bin/ssh-text-entrypoint

EXPOSE 22 7910 7911

ENTRYPOINT ["tini", "--", "/usr/local/bin/ssh-text-entrypoint"]
CMD ["node", "src/server/server.js"]
