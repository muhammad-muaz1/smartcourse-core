#!/usr/bin/env bash
# Local infrastructure without Docker: downloads an official release into .tools/ (once,
# checksum-verified) and runs it in the foreground, state kept under .tools/. The Docker
# path (`make up`) runs the same components from images instead.
#
#   scripts/dev_infra.sh kafka        # Kafka (KRaft, single node) on :9092 — needs Java 17+
#   scripts/dev_infra.sh jaeger       # Jaeger v2: UI :16686, OTLP gRPC :4317 / HTTP :4318
#   scripts/dev_infra.sh prometheus   # Prometheus on :9090, scraping deploy/observability
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/.tools"
mkdir -p "$TOOLS"

KAFKA_VERSION=4.4.0
KAFKA_SCALA=2.13
JAEGER_VERSION=2.23.0
PROMETHEUS_VERSION=3.16.0

# download <url> <dest> <expected-sha-line-or-empty> <algo>
download() {
  local url="$1" dest="$2" expected="$3" algo="$4"
  if [ -f "$dest" ]; then return; fi
  echo "downloading $url"
  curl -fL --retry 3 -o "$dest.part" "$url"
  if [ -n "$expected" ]; then
    local actual
    actual="$("${algo}sum" "$dest.part" | awk '{print $1}')"
    if [ "$actual" != "$expected" ]; then
      rm -f "$dest.part"
      echo "checksum mismatch for $url" >&2
      exit 1
    fi
  fi
  mv "$dest.part" "$dest"
}

kafka() {
  local name="kafka_${KAFKA_SCALA}-${KAFKA_VERSION}"
  local base="https://downloads.apache.org/kafka/${KAFKA_VERSION}/${name}.tgz"
  local home="$TOOLS/$name"
  if [ ! -d "$home" ]; then
    # Apache publishes the sha512 as "file: HEX HEX ..." across several lines.
    local expected
    expected="$(curl -fsL "$base.sha512" | tr -d ' \n' | cut -d: -f2 | tr 'A-F' 'a-f')"
    download "$base" "$TOOLS/$name.tgz" "$expected" sha512
    tar -xzf "$TOOLS/$name.tgz" -C "$TOOLS"
  fi

  local data="$TOOLS/kafka-data"
  local config="$TOOLS/kafka-server.properties"
  if [ ! -f "$config" ]; then
    # Stock single-node KRaft config, with its log dir moved out of /tmp so topics
    # (and the replayable history analytics depends on) survive a reboot.
    sed "s#^log.dirs=.*#log.dirs=$data#" "$home/config/server.properties" > "$config"
    echo "auto.create.topics.enable=false" >> "$config"
  fi
  if [ ! -f "$data/meta.properties" ]; then
    "$home/bin/kafka-storage.sh" format --standalone \
      -t "$("$home/bin/kafka-storage.sh" random-uuid)" -c "$config"
  fi
  exec "$home/bin/kafka-server-start.sh" "$config"
}

jaeger() {
  local name="jaeger-${JAEGER_VERSION}-linux-amd64"
  local base="https://github.com/jaegertracing/jaeger/releases/download/v${JAEGER_VERSION}"
  if [ ! -x "$TOOLS/$name/jaeger" ]; then
    download "$base/${name}.tar.gz" "$TOOLS/${name}.tar.gz" "" sha256
    tar -xzf "$TOOLS/${name}.tar.gz" -C "$TOOLS"
    # Jaeger's checksum file covers the extracted binaries, not the tarball.
    local expected actual
    expected="$(curl -fsL "$base/${name}.sha256sum.txt" | grep "\*${name}/jaeger\$" | awk '{print $1}')"
    actual="$(sha256sum "$TOOLS/$name/jaeger" | awk '{print $1}')"
    if [ -z "$expected" ] || [ "$actual" != "$expected" ]; then
      rm -rf "$TOOLS/$name" "$TOOLS/${name}.tar.gz"
      echo "checksum mismatch for jaeger binary" >&2
      exit 1
    fi
  fi
  # No config = all-in-one with in-memory storage: OTLP in on :4317/:4318, UI on :16686.
  exec "$TOOLS/$name/jaeger"
}

prometheus() {
  local name="prometheus-${PROMETHEUS_VERSION}.linux-amd64"
  local base="https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}"
  if [ ! -x "$TOOLS/$name/prometheus" ]; then
    local expected
    expected="$(curl -fsL "$base/sha256sums.txt" | grep " ${name}.tar.gz\$" | awk '{print $1}')"
    download "$base/${name}.tar.gz" "$TOOLS/${name}.tar.gz" "$expected" sha256
    tar -xzf "$TOOLS/${name}.tar.gz" -C "$TOOLS"
  fi
  exec "$TOOLS/$name/prometheus" \
    --config.file="$ROOT/deploy/observability/prometheus.local.yml" \
    --storage.tsdb.path="$TOOLS/prometheus-data" \
    --web.listen-address=":9090"
}

case "${1:-}" in
  kafka) kafka ;;
  jaeger) jaeger ;;
  prometheus) prometheus ;;
  *) echo "usage: $0 kafka|jaeger|prometheus" >&2; exit 2 ;;
esac
