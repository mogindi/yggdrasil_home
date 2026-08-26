#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_SCRIPT="$SCRIPT_DIR/omarchy-image-builder.sh"
GUEST_SCRIPT="$SCRIPT_DIR/omarchy-image-guest.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

bash -n "$HOST_SCRIPT" || fail "host builder has invalid shell syntax"
bash -n "$GUEST_SCRIPT" || fail "guest builder has invalid shell syntax"
grep -Fq -- 'hw_qemu_guest_agent=yes' "$HOST_SCRIPT" || fail "upload metadata omits qemu guest agent"
grep -Fq -- 'cloud-init' "$GUEST_SCRIPT" || fail "guest builder omits cloud-init"
grep -Fq -- 'qemu-guest-agent' "$GUEST_SCRIPT" || fail "guest builder omits qemu guest agent"
grep -Fq -- '"removable": true' "$GUEST_SCRIPT" || fail "guest bootloader lacks UEFI fallback"

help_output="$(bash "$HOST_SCRIPT" --help)"
grep -Fq -- '--no-upload' <<<"$help_output" || fail "help omits --no-upload"
grep -Fq -- 'OMARCHY_ISO_URL' <<<"$help_output" || fail "help omits ISO override"
grep -Fq -- 'OMARCHY_LXC_IMAGE_SOURCE' <<<"$help_output" || fail "help omits LXC image override"

dry_run_output="$(
  OMARCHY_ISO_URL=https://iso.omarchy.org/omarchy-4.0.1.iso \
  OMARCHY_ISO_SHA256_URL=https://iso.omarchy.org/omarchy-4.0.1.iso.sha256 \
  bash "$HOST_SCRIPT" --dry-run --no-upload
)"
grep -Fq -- 'OMARCHY_ISO_VERSION=4.0.1' <<<"$dry_run_output" || fail "dry-run did not resolve the ISO version"
grep -Fq -- 'OMARCHY_IMAGE_NAME=omarchy-latest' <<<"$dry_run_output" || fail "dry-run did not show the default image name"
grep -Fq -- 'OMARCHY_OUTPUT_FORMAT=raw' <<<"$dry_run_output" || fail "dry-run did not show the default output format"
grep -Fq -- 'OMARCHY_LXC_IMAGE_SOURCE=ubuntu:24.04' <<<"$dry_run_output" || fail "dry-run did not show the default LXC image"

if OMARCHY_OUTPUT_FORMAT=invalid \
  OMARCHY_ISO_URL=https://iso.omarchy.org/omarchy-4.0.1.iso \
  OMARCHY_ISO_SHA256_URL=https://iso.omarchy.org/omarchy-4.0.1.iso.sha256 \
  bash "$HOST_SCRIPT" --dry-run --no-upload >/dev/null 2>&1; then
  fail "invalid output format was accepted"
fi

if OMARCHY_DISK_SIZE=20G \
  OMARCHY_ISO_URL=https://iso.omarchy.org/omarchy-4.0.1.iso \
  OMARCHY_ISO_SHA256_URL=https://iso.omarchy.org/omarchy-4.0.1.iso.sha256 \
  bash "$HOST_SCRIPT" --dry-run --no-upload >/dev/null 2>&1; then
  fail "an undersized Omarchy disk was accepted"
fi

echo "PASS: Omarchy image builder static checks"
