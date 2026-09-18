#!/usr/bin/env bash
# Install pinned tools on a GitHub-hosted runner, each checked against the
# SHA-256 GitHub records for that release asset (the `digest` field of
# GET /repos/{owner}/{repo}/releases/tags/{tag}, read 2026-09-18).
#
#   ci/install_tools.sh BIN_DIR TOOL...     TOOL: age gitleaks osv-scanner xcodegen
#
# Picks the linux-amd64 or darwin-arm64 asset from `uname`. A digest mismatch
# stops the job: a tool that is not the one pinned here never runs.
set -euo pipefail

bin_dir=$1
shift
mkdir -p "$bin_dir"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64) plat=linux ;;
  Darwin-arm64) plat=darwin ;;
  *) echo "unsupported runner: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

fetch() { # url sha256 dest
  curl --fail --silent --show-error --location --retry 3 --output "$3" "$1"
  echo "$2  $3" | shasum -a 256 --check --status || { echo "checksum mismatch for $1" >&2; exit 1; }
}

for tool in "$@"; do
  case "$tool-$plat" in
    age-linux)
      fetch https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz \
        cbe24006683f8eb669266162894b9a522a1af52f2665fbc63a4bb032ed26ac10 "$work/age.tgz"
      tar -xzf "$work/age.tgz" -C "$work" && install -m 0755 "$work/age/age" "$work/age/age-keygen" "$bin_dir/" ;;
    age-darwin)
      fetch https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-darwin-arm64.tar.gz \
        e2020b073c44f692685a24d6abc378817eb81ffaaf49fd0531ef8565f767f2f5 "$work/age.tgz"
      tar -xzf "$work/age.tgz" -C "$work" && install -m 0755 "$work/age/age" "$work/age/age-keygen" "$bin_dir/" ;;
    gitleaks-linux)
      fetch https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
        551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb "$work/gitleaks.tgz"
      tar -xzf "$work/gitleaks.tgz" -C "$work" gitleaks && install -m 0755 "$work/gitleaks" "$bin_dir/" ;;
    osv-scanner-linux)
      fetch https://github.com/google/osv-scanner/releases/download/v2.6.0/osv-scanner_linux_amd64 \
        ca69b3d3cd08f889a49dc0a383122f71cc528b83803671df5fd874d97485b108 "$work/osv-scanner"
      install -m 0755 "$work/osv-scanner" "$bin_dir/" ;;
    xcodegen-darwin)
      fetch https://github.com/yonaskolb/XcodeGen/releases/download/2.46.0/xcodegen.zip \
        4d9e34b62172d645eed6457cac13fc222569974098ef4ee9c3368bedf0196806 "$work/xcodegen.zip"
      # The zip holds bin/xcodegen plus the share/ templates it reads beside it.
      mkdir -p "$bin_dir/../xcodegen" && ditto -x -k "$work/xcodegen.zip" "$bin_dir/../xcodegen"
      ln -sf "$(cd "$bin_dir/../xcodegen" && pwd)/xcodegen/bin/xcodegen" "$bin_dir/xcodegen" ;;
    *) echo "no pinned $tool for $plat" >&2; exit 1 ;;
  esac
  echo "installed $tool"
done
