# OpenMontage as a Celaut service, linux/amd64. nodo builds it with --opt
# platform=linux/amd64 (from service.json).
#
# Upstream: calesthio/OpenMontage (AGPL-3.0), fetched by commit (the same commit as the
# submodule upstream/ of this repository; tests/test_layout.py checks that they agree).

# ---- OpenMontage at a fixed commit, without the parts that the service does not use ----
# The base is pinned by the digest of the multi-arch index of debian:bookworm-slim (the pin
# of celaut-basics/echo-chat). apt reads a frozen snapshot of the Debian archive.
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS upstream
ARG DEBIAN_SNAPSHOT=20261004T000000Z
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      -e '/^URIs: /a Check-Valid-Until: no' \
      /etc/apt/sources.list.d/debian.sources \
    && [ "$(grep -c '^URIs: http://snapshot.debian.org/' /etc/apt/sources.list.d/debian.sources)" = 2 ] \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG OPENMONTAGE_COMMIT=d21f5d4eaa08422b09c7091757a05affcbabb35b
RUN git init -q /src && cd /src \
    && git fetch -q --depth 1 https://github.com/calesthio/OpenMontage.git "${OPENMONTAGE_COMMIT}" \
    && git checkout -q FETCH_HEAD \
    && mkdir /opt/openmontage \
    && cp -a LICENSE README.md config.yaml lib tools styles schemas remotion-composer /opt/openmontage/ \
    && echo "${OPENMONTAGE_COMMIT}" > /opt/openmontage/UPSTREAM_COMMIT \
    && rm -rf /src

# ---- Remotion: the node modules of remotion-composer (npm ci, from its lock file), the
# headless Chromium that Remotion uses, and a bundle of the composition ----
FROM node:22-bookworm-slim@sha256:5ec23b7c622b00547753bc6ffd78085dd868a68aeb41c8e0a1418f033e13d96d AS remotion
COPY --from=upstream /opt/openmontage/remotion-composer /opt/openmontage/remotion-composer
WORKDIR /opt/openmontage/remotion-composer
ENV REMOTION_DISABLE_TELEMETRY=1 NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false
RUN npm ci --no-audit > /tmp/npm-ci.log 2>&1 || (tail -40 /tmp/npm-ci.log; exit 1)
# chrome-headless-shell into node_modules/.remotion, where `remotion render` looks for it.
RUN npx --no-install remotion browser ensure > /tmp/browser.log 2>&1 || (tail -20 /tmp/browser.log; exit 1)
# The bundle (webpack output) of src/index.tsx: a render reads it and needs no webpack.
RUN npx --no-install remotion bundle src/index.tsx --out-dir=/opt/remotion-bundle > /tmp/bundle.log 2>&1 \
    || (tail -40 /tmp/bundle.log; exit 1)

# ---- the service ----
# python:3.11-slim-bookworm, pinned by the digest of its multi-arch index (the pin of
# celaut-basics/distributed-llama). Same Debian (bookworm) as the stages above.
FROM python:3.11-slim-bookworm@sha256:94a8fee35252d06ab75856f15f12c7bae67b10e8a6da93c7fded82b2610926cd
SHELL ["/bin/bash", "-c"]
ARG DEBIAN_SNAPSHOT=20261004T000000Z
# FFmpeg (cuts, scales, encodes; Debian builds it with libx264, libmp3lame and aac), the
# libraries of Remotion's headless Chromium (the list of remotion.dev/docs/miscellaneous/
# linux-dependencies, Debian names) and fonts.
RUN sed -i \
      -e "s|^URIs: http://deb.debian.org/debian\$|URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}|" \
      -e "s|^URIs: http://deb.debian.org/debian-security\$|URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}|" \
      -e '/^URIs: /a Check-Valid-Until: no' \
      /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install --no-install-recommends -y \
        ffmpeg ca-certificates fonts-dejavu-core \
        libnss3 libdbus-1-3 libatk1.0-0 libgbm1 libasound2 libxrandr2 libxkbcommon0 \
        libxfixes3 libxcomposite1 libxdamage1 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 \
        libcups2 libdrm2 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 om \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin om
# gRPC and protobuf for the chat interface (stubs of app/ made with grpcio-tools 1.85.1,
# scripts/gen_proto.sh), requests for OpenMontage's stock sources and the LLM, and Piper
# (piper-tts, GPL-3.0-or-later, offline TTS on onnxruntime).
COPY service/app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt > /tmp/pip.log 2>&1 || (tail -30 /tmp/pip.log; exit 1) \
    && pip3 freeze > /opt/pip-freeze.txt && rm -f /tmp/pip.log
# The voice: Piper en_US-joe-medium (dataset CC0, OHF-Voice/voice-datasets), by revision
# of rhasspy/piper-voices and sha256.
ADD --checksum=sha256:58afce0321b8d9c46d7cdf9c16500cc55a793b4220212dba6b70fb788b3baf06 \
    https://huggingface.co/rhasspy/piper-voices/resolve/e7229f7d33a10e145e109b886d661984201ac3c4/en/en_US/joe/medium/en_US-joe-medium.onnx \
    /opt/piper/en_US-joe-medium.onnx
ADD --checksum=sha256:3d6d5410b3795cb1950595247ef8f06190719e6fdbfa3a2356d8ec368e1aad33 \
    https://huggingface.co/rhasspy/piper-voices/resolve/e7229f7d33a10e145e109b886d661984201ac3c4/en/en_US/joe/medium/en_US-joe-medium.onnx.json \
    /opt/piper/en_US-joe-medium.onnx.json
COPY --from=remotion /usr/local/bin/node /usr/local/bin/node
COPY --from=upstream /opt/openmontage /opt/openmontage
COPY --from=remotion /opt/openmontage/remotion-composer/node_modules /opt/openmontage/remotion-composer/node_modules
COPY --from=remotion /opt/remotion-bundle /opt/remotion-bundle
# service/ holds app/ (the packer puts the `include` paths of pack_config.json there).
COPY service /service
# (Only these two: a chmod -R of node_modules copies every file up in the overlay and
# takes minutes; npm and git already write them world-readable.)
RUN chmod -R a+rX /service /opt/piper \
    && find /service -name '__pycache__' -prune -exec rm -rf {} +
ENV OPENMONTAGE_UPSTREAM=/opt/openmontage \
    OPENMONTAGE_REMOTION_BUNDLE=/opt/remotion-bundle \
    OPENMONTAGE_VOICE_MODEL=/opt/piper/en_US-joe-medium.onnx \
    OPENMONTAGE_WORK_DIR=/work \
    REMOTION_DISABLE_TELEMETRY=1 \
    PYTHONUNBUFFERED=1
# The entrypoint (service.json init.entry_path). The guest init starts it as PID 1 and as
# root. As root it writes a DNS resolver (app/resolver.py) and makes the work directory on
# the disk of the instance, then starts the service as the user om.
RUN printf '%s\n' \
        '#!/bin/sh' \
        'cd /service' \
        '/usr/local/bin/python3 -E -s -m app.resolver || exit 2' \
        'mkdir -p /work && chown 10001:10001 /work && chmod 0700 /work' \
        'export OPENMONTAGE_UPSTREAM_COMMIT="$(cat /opt/openmontage/UPSTREAM_COMMIT)"' \
        'export HOME=/work PATH=/usr/local/bin:/usr/bin:/bin' \
        'exec setpriv --reuid 10001 --regid 10001 --clear-groups --no-new-privs /usr/local/bin/python3 -m app.main' \
        > /entrypoint.sh \
    && chmod 0755 /entrypoint.sh
# Build-time smoke test, as the service user and with no network: the chat answers, Piper
# speaks, and Remotion renders a 3-scene video of cards. An image that cannot render does
# not become a service id.
# (SMOKE=0 skips it, for a debug build only; nodo packs with the default.)
ARG SMOKE=1
RUN [ "$SMOKE" = 0 ] || { mkdir -p /tmp/smoke && chown 10001:10001 /tmp/smoke && cd /service \
    && OPENMONTAGE_WORK_DIR=/tmp/smoke HOME=/tmp/smoke \
       setpriv --reuid 10001 --regid 10001 --clear-groups /usr/local/bin/python3 -m app.smoke \
    && rm -rf /tmp/smoke; }
