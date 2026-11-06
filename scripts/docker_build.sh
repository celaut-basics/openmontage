#!/bin/sh
# Build the image with plain Docker, in the layout of nodo's packer: the build context has
# the Dockerfile and service/<include> (pack_config.json `include`), symlinks resolved.
#   scripts/docker_build.sh [tag]   (default openmontage:dev)
set -eu
cd "$(dirname "$0")/.."
TAG=${1:-openmontage:dev}
CTX=$(mktemp -d)
trap 'rm -rf "$CTX"' EXIT
mkdir -p "$CTX/service"
cp -RL app "$CTX/service/app"
find "$CTX" -name '__pycache__' -prune -exec rm -rf {} +
cp amd64/.service/Dockerfile "$CTX/Dockerfile"
# ADD --checksum needs a recent BuildKit (Docker 24 has 0.11): use a docker-container builder.
docker buildx inspect om-builder >/dev/null 2>&1 || docker buildx create --name om-builder --driver docker-container >/dev/null
docker buildx build --builder om-builder --platform linux/amd64 --progress=plain --load \
    --build-arg SMOKE="${SMOKE:-1}" -t "$TAG" "$CTX"
