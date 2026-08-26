#!/usr/bin/env bash

set -Eeuo pipefail

WORK_DIR="${OMARCHY_BUILD_DIR:-/root/omarchy-build}"
ISO_URL="${OMARCHY_ISO_URL:?OMARCHY_ISO_URL is required}"
ISO_SHA256_URL="${OMARCHY_ISO_SHA256_URL:?OMARCHY_ISO_SHA256_URL is required}"
ISO_VERSION="${OMARCHY_ISO_VERSION:?OMARCHY_ISO_VERSION is required}"
DISK_SIZE="${OMARCHY_DISK_SIZE:-50G}"
OUTPUT_FORMAT="${OMARCHY_OUTPUT_FORMAT:-raw}"
GUEST_CPUS="${OMARCHY_GUEST_CPUS:-4}"
GUEST_MEMORY="${OMARCHY_GUEST_MEMORY:-8G}"
INSTALL_FIRMWARE="${OMARCHY_INSTALL_FIRMWARE:-bios}"
INSTALL_TIMEOUT="${OMARCHY_INSTALL_TIMEOUT:-30m}"
GUEST_BOOT_TIMEOUT="${OMARCHY_GUEST_BOOT_TIMEOUT:-20m}"
ALLOW_TCG="${OMARCHY_ALLOW_TCG:-0}"

ISO_FILE="$WORK_DIR/omarchy-$ISO_VERSION.iso"
ISO_CHECKSUM_FILE="$WORK_DIR/omarchy-$ISO_VERSION.iso.sha256"
DISK_FILE="$WORK_DIR/omarchy-disk.qcow2"
CIDATA_DIR="$WORK_DIR/cidata"
CIDATA_FILE="$WORK_DIR/cidata.iso"
SSH_KEY="$WORK_DIR/builder-key"
SSH_PORT=2222
QGA_SOCKET="$WORK_DIR/qga.sock"
INSTALL_LOG="$WORK_DIR/omarchy-installer.log"
GUEST_LOG="$WORK_DIR/omarchy-guest.log"
POSTINSTALL_SCRIPT="$WORK_DIR/postinstall.sh"
ARTIFACT_FILE="$WORK_DIR/omarchy-image.$OUTPUT_FORMAT"
METADATA_FILE="$WORK_DIR/build-metadata.env"

QEMU_PID=""
QEMU_ACCEL_ARGS=()

die() {
  echo "ERROR: $*" >&2
  exit 1
}

log() {
  echo "==> $*"
}

command_required() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

valid_size() {
  [[ "$1" =~ ^[1-9][0-9]*(G|GiB|GB|M|MiB|MB)$ ]]
}

size_in_mib() {
  local size="$1"
  local value="${size//[!0-9]/}"
  local unit="${size//[0-9]/}"
  case "$unit" in
    G|GiB|GB)
      printf '%s\n' "$((value * 1024))"
      ;;
    M|MiB|MB)
      printf '%s\n' "$value"
      ;;
    *)
      return 1
      ;;
  esac
}

cleanup() {
  local exit_code=$?
  if [[ -n "$QEMU_PID" ]] && kill -0 "$QEMU_PID" >/dev/null 2>&1; then
    kill "$QEMU_PID" >/dev/null 2>&1 || true
    wait "$QEMU_PID" >/dev/null 2>&1 || true
  fi
  rm -f -- "$QGA_SOCKET" "$CIDATA_FILE" "$SSH_KEY" "$SSH_KEY.pub" "$POSTINSTALL_SCRIPT"
  exit "$exit_code"
}
trap cleanup EXIT

find_ovmf_file() {
  local candidate
  for candidate in "$@"; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

download_iso() {
  mkdir -p "$WORK_DIR"
  chmod 0700 "$WORK_DIR"
  if [[ ! -s "$ISO_FILE" ]]; then
    log "Downloading Omarchy $ISO_VERSION"
    curl --fail --silent --show-error --location --retry 5 --retry-all-errors \
      --output "$ISO_FILE" "$ISO_URL"
  fi

  log "Verifying the Omarchy ISO checksum"
  curl --fail --silent --show-error --location --retry 5 --retry-all-errors \
    --output "$ISO_CHECKSUM_FILE" "$ISO_SHA256_URL"
  local expected actual
  expected="$(awk 'NF { print $1; exit }' "$ISO_CHECKSUM_FILE")"
  [[ "$expected" =~ ^[[:xdigit:]]{64}$ ]] || die "Checksum file did not contain a SHA-256 digest"
  actual="$(sha256sum "$ISO_FILE" | awk '{print $1}')"
  [[ "${actual,,}" == "${expected,,}" ]] ||
    die "Omarchy ISO checksum mismatch: expected $expected, got $actual"
}

create_cidata() {
  local disk_bytes boot_size main_start main_size disk_size_gib password_hash
  disk_bytes="$(qemu-img info --output=json "$DISK_FILE" | jq -r '.["virtual-size"]')"
  [[ "$disk_bytes" =~ ^[0-9]+$ ]] || die "Unable to determine nested disk size"
  boot_size=$((2 * 1024 * 1024 * 1024))
  main_start=$((boot_size + 1024 * 1024))
  main_size=$((disk_bytes - main_start - 1024 * 1024))
  ((main_size > 0)) || die "Nested disk is too small for Omarchy"
  disk_size_gib=$(( (disk_bytes + 1024 * 1024 * 1024 - 1) / (1024 * 1024 * 1024) ))

  local builder_password
  builder_password="$(openssl rand -hex 32)"
  password_hash="$(printf '%s' "$builder_password" | openssl passwd -6 -stdin)"

  rm -rf -- "$CIDATA_DIR"
  mkdir -p "$CIDATA_DIR"
  chmod 0700 "$CIDATA_DIR"

  jq -n \
    --arg disk /dev/vda \
    --arg hostname omarchy \
    --arg config_version "${OMARCHY_ARCHINSTALL_CONFIG_VERSION:-3.0.9}" \
    --argjson boot_size "$boot_size" \
    --argjson main_start "$main_start" \
    --argjson main_size "$main_size" \
    '{
      "app_config": null,
      "archinstall-language": "English",
      "auth_config": {},
      "audio_config": {"audio": "pipewire"},
      "bootloader_config": {"bootloader": "Limine", "uki": false, "removable": true},
      "custom_commands": [],
      "omarchy_install": {
        "mode": "full_disk",
        "defer_provisioning": false,
        "target_mount": "/mnt",
        "boot": {"esp_mount": "/boot", "esp_path": "/EFI/limine", "efi_binary": "limine_x64.efi", "enable_fallback": true},
        "storage": {"kernel": "linux"}
      },
      "disk_config": {
        "config_type": "default_layout",
        "device_modifications": [{
          "device": $disk,
          "partitions": [
            {
              "btrfs": [],
              "dev_path": null,
              "flags": ["boot", "esp"],
              "fs_type": "fat32",
              "mount_options": [],
              "mountpoint": "/boot",
              "obj_id": "ea21d3f2-82bb-49cc-ab5d-6f81ae94e18d",
              "size": {"sector_size": {"unit": "B", "value": 512}, "unit": "B", "value": $boot_size},
              "start": {"sector_size": {"unit": "B", "value": 512}, "unit": "B", "value": 1048576},
              "status": "create",
              "type": "primary"
            },
            {
              "btrfs": [
                {"mountpoint": "/", "name": "@"},
                {"mountpoint": "/home", "name": "@home"},
                {"mountpoint": "/var/log", "name": "@log"},
                {"mountpoint": "/var/cache/pacman/pkg", "name": "@pkg"}
              ],
              "dev_path": null,
              "flags": [],
              "fs_type": "btrfs",
              "mount_options": ["compress=zstd"],
              "mountpoint": null,
              "obj_id": "8c2c2b92-1070-455d-b76a-56263bab24aa",
              "size": {"sector_size": {"unit": "B", "value": 512}, "unit": "B", "value": $main_size},
              "start": {"sector_size": {"unit": "B", "value": 512}, "unit": "B", "value": $main_start},
              "status": "create",
              "type": "primary"
            }
          ],
          "wipe": true
        }]
      },
      "hostname": $hostname,
      "kernels": ["linux"],
      "network_config": {"type": "iso"},
      "ntp": true,
      "parallel_downloads": 8,
      "script": null,
      "services": [],
      "swap": true,
      "timezone": "UTC",
      "locale_config": {"kb_layout": "us", "sys_enc": "UTF-8", "sys_lang": "en_US.UTF-8"},
      "mirror_config": {
        "custom_repositories": [],
        "custom_servers": [
          {"url": "https://mirror.omarchy.org/$repo/os/$arch"},
          {"url": "https://mirror.rackspace.com/archlinux/$repo/os/$arch"},
          {"url": "https://geo.mirror.pkgbuild.com/$repo/os/$arch"}
        ],
        "mirror_regions": {},
        "optional_repositories": []
      },
      "packages": ["base-devel", "git", "openssh", "cloud-init", "qemu-guest-agent", "omarchy-keyring", "omarchy-settings", "omarchy"],
      "profile_config": {"gfx_driver": null, "greeter": null, "profile": {}},
      "version": $config_version
    }' > "$CIDATA_DIR/user_configuration.json"

  jq -n \
    --arg password_hash "$password_hash" \
    --arg username omarchy \
    '{"root_enc_password": $password_hash, "users": [{"enc_password": $password_hash, "groups": [], "sudo": true, "username": $username}]}' \
    > "$CIDATA_DIR/user_credentials.json"

  : > "$CIDATA_DIR/user_full_name.txt"
  : > "$CIDATA_DIR/user_email_address.txt"
  printf 'false\n' > "$CIDATA_DIR/user_encrypt_installation.txt"
  ssh-keygen -q -t ed25519 -N '' -C omarchy-image-builder -f "$SSH_KEY"
  cp "$SSH_KEY.pub" "$CIDATA_DIR/authorized_keys"

  genisoimage -quiet -output "$CIDATA_FILE" -volid cidata -joliet -rock "$CIDATA_DIR"
  chmod 0600 "$CIDATA_FILE"
  printf '%s\n' "$disk_size_gib" > "$WORK_DIR/disk-size-gib"
}

write_postinstall_script() {
  cat > "$POSTINSTALL_SCRIPT" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

pacman -Syu --noconfirm cloud-init qemu-guest-agent openssh

install -d -m 0755 /etc/cloud/cloud.cfg.d
rm -f /etc/cloud/cloud-init.disabled
cat > /etc/cloud/cloud.cfg.d/90-yggdrasil-openstack.cfg <<'YGGDRASIL_CLOUD'
datasource_list: [ OpenStack, ConfigDrive, NoCloud, None ]
ssh_pwauth: true
system_info:
  default_user:
    name: omarchy
    gecos: Omarchy User
    lock_passwd: false
    groups: [adm, audio, input, optical, storage, video, wheel]
    sudo: ["ALL=(ALL) NOPASSWD: ALL"]
    shell: /bin/zsh
YGGDRASIL_CLOUD

install -d -m 0755 /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-yggdrasil-cloud-init.conf <<'YGGDRASIL_SSH'
PasswordAuthentication yes
KbdInteractiveAuthentication yes
UsePAM yes
PermitRootLogin no
YGGDRASIL_SSH

systemctl enable sshd.service
systemctl enable qemu-guest-agent.service
for unit in cloud-init-local.service cloud-init.service cloud-config.service cloud-final.service; do
  systemctl enable "$unit"
done
systemctl restart sshd.service
systemctl restart qemu-guest-agent.service
systemctl is-active --quiet qemu-guest-agent.service

# The password is intentionally locked in the golden image. The existing
# Yggdrasil console password user-data unlocks it with chpasswd at first boot;
# SSH keys remain usable while the password is locked.
passwd -l omarchy
rm -f /home/omarchy/.ssh/authorized_keys /home/omarchy/.ssh/known_hosts
rmdir /home/omarchy/.ssh 2>/dev/null || true
rm -f /root/.ssh/authorized_keys

cloud-init clean --logs --seed
rm -f /etc/ssh/ssh_host_* /etc/machine-id /var/lib/dbus/machine-id
rm -rf /var/lib/cloud/instances /var/lib/cloud/sem
rm -f /home/omarchy/.bash_history /root/.bash_history
sync
EOF
  chmod 0700 "$POSTINSTALL_SCRIPT"
}

qemu_common_args() {
  local firmware_code="${1:-}"
  local firmware_vars="${2:-}"
  local disk_args=(
    -machine q35
    -m "$GUEST_MEMORY"
    -smp "$GUEST_CPUS"
    -drive "file=$DISK_FILE,if=virtio,format=qcow2,cache=none"
    -vga virtio
    -display none
  )
  if [[ "$INSTALL_FIRMWARE" == uefi ]]; then
    disk_args+=(
      -drive "if=pflash,format=raw,readonly=on,file=$firmware_code"
      -drive "if=pflash,format=raw,file=$firmware_vars"
    )
  fi
  printf '%s\n' "${disk_args[@]}"
}

qemu_acceleration_args() {
  if [[ -e /dev/kvm ]]; then
    QEMU_ACCEL_ARGS=(-enable-kvm -cpu host)
  elif [[ "$ALLOW_TCG" == 1 ]]; then
    echo "WARNING: /dev/kvm is unavailable; using software emulation" >&2
    QEMU_ACCEL_ARGS=(-cpu max)
  else
    die "Nested KVM is unavailable in the LXC VM; set OMARCHY_ALLOW_TCG=1 to permit slow software emulation"
  fi
}

duration_seconds() {
  local duration="$1"
  if [[ "$duration" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$duration"
  elif [[ "$duration" =~ ^([0-9]+)(s|m|h)$ ]]; then
    case "${BASH_REMATCH[2]}" in
      s) printf '%s\n' "${BASH_REMATCH[1]}" ;;
      m) printf '%s\n' "$(( BASH_REMATCH[1] * 60 ))" ;;
      h) printf '%s\n' "$(( BASH_REMATCH[1] * 3600 ))" ;;
    esac
  else
    die "Timeout must be expressed as seconds, minutes, or hours (for example 20m)"
  fi
}

run_installer_vm() {
  local firmware_code="$1"
  local firmware_vars="$2"
  local -a args
  mapfile -t args < <(qemu_common_args "$firmware_code" "$firmware_vars")
  qemu_acceleration_args
  args+=(
    "${QEMU_ACCEL_ARGS[@]}"
    -name omarchy-installer
    -drive "file=$ISO_FILE,if=ide,media=cdrom,readonly=on"
    -drive "file=$CIDATA_FILE,if=ide,media=cdrom,readonly=on"
    -boot order=d
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22"
    -device virtio-net-pci,netdev=net0
    -serial "file:$INSTALL_LOG"
    -no-reboot
  )

  log "Running the unattended Omarchy installer"
  set +e
  timeout --foreground "$INSTALL_TIMEOUT" qemu-system-x86_64 "${args[@]}" > "$GUEST_LOG" 2>&1
  local qemu_status=$?
  set -e
  ((qemu_status != 124)) || die "Omarchy installer exceeded $INSTALL_TIMEOUT; inspect $INSTALL_LOG"
  ((qemu_status == 0)) || die "Omarchy installer QEMU exited with status $qemu_status; inspect $INSTALL_LOG and $GUEST_LOG"
}

ssh_guest() {
  ssh -i "$SSH_KEY" -p "$SSH_PORT" \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    "omarchy@127.0.0.1" "$@"
}

wait_for_guest_ssh() {
  local timeout_seconds
  timeout_seconds="$(duration_seconds "$GUEST_BOOT_TIMEOUT")"
  local deadline=$((SECONDS + timeout_seconds))
  while ((SECONDS < deadline)); do
    if [[ -n "$QEMU_PID" ]] && ! kill -0 "$QEMU_PID" >/dev/null 2>&1; then
      die "Omarchy guest stopped before SSH became available; inspect $GUEST_LOG"
    fi
    if ssh_guest true >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  die "Omarchy guest SSH did not become available within $GUEST_BOOT_TIMEOUT"
}

run_configured_guest() {
  local firmware_code="$1"
  local firmware_vars="$2"
  local -a args
  mapfile -t args < <(qemu_common_args "$firmware_code" "$firmware_vars")
  qemu_acceleration_args
  args+=(
    "${QEMU_ACCEL_ARGS[@]}"
    -name omarchy-guest
    -boot order=c
    -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$SSH_PORT-:22"
    -device virtio-net-pci,netdev=net0
    -chardev "socket,id=qga0,path=$QGA_SOCKET,server=on,wait=off"
    -device virtio-serial
    -device virtserialport,chardev=qga0,name=org.qemu.guest_agent.0
    -serial "file:$GUEST_LOG"
  )

  rm -f "$QGA_SOCKET"
  qemu-system-x86_64 "${args[@]}" > "$WORK_DIR/omarchy-guest-console.log" 2>&1 &
  QEMU_PID=$!
  wait_for_guest_ssh
}

configure_guest() {
  write_postinstall_script
  ssh_guest 'cat > /tmp/yggdrasil-omarchy-postinstall.sh' < "$POSTINSTALL_SCRIPT"
  ssh_guest 'chmod 0700 /tmp/yggdrasil-omarchy-postinstall.sh && sudo /tmp/yggdrasil-omarchy-postinstall.sh'
  ssh_guest 'sudo systemctl poweroff' >/dev/null 2>&1 || true

  local attempt
  for attempt in $(seq 1 60); do
    if ! kill -0 "$QEMU_PID" >/dev/null 2>&1; then
      QEMU_PID=""
      return 0
    fi
    if [[ "$(ps -o stat= -p "$QEMU_PID" 2>/dev/null | awk '{print $1}')" == Z* ]]; then
      wait "$QEMU_PID" >/dev/null 2>&1 || true
      QEMU_PID=""
      return 0
    fi
    sleep 2
  done
  kill "$QEMU_PID" >/dev/null 2>&1 || true
  wait "$QEMU_PID" >/dev/null 2>&1 || true
  QEMU_PID=""
}

export_artifact() {
  log "Checking and exporting the cleaned Omarchy disk"
  qemu-img check "$DISK_FILE"
  case "$OUTPUT_FORMAT" in
    raw)
      qemu-img convert -p -O raw "$DISK_FILE" "$ARTIFACT_FILE"
      ;;
    qcow2)
      qemu-img convert -p -O qcow2 "$DISK_FILE" "$ARTIFACT_FILE"
      ;;
  esac
  chmod 0600 "$ARTIFACT_FILE"

  local disk_size_gib
  disk_size_gib="$(cat "$WORK_DIR/disk-size-gib")"
  printf 'OMARCHY_VERSION=%s\nIMAGE_FORMAT=%s\nDISK_SIZE_GIB=%s\n' \
    "$ISO_VERSION" "$OUTPUT_FORMAT" "$disk_size_gib" > "$METADATA_FILE"
  chmod 0600 "$METADATA_FILE"

  # The cidata image and build key contain temporary credentials. Remove them
  # before returning even when the LXC VM is retained for troubleshooting.
  rm -rf -- "$CIDATA_DIR" "$ISO_CHECKSUM_FILE" "$DISK_FILE" "$ISO_FILE"
  rm -f -- "$SSH_KEY" "$SSH_KEY.pub" "$POSTINSTALL_SCRIPT"
}

main() {
  command_required awk
  command_required cat
  command_required chmod
  command_required cloud-init
  command_required curl
  command_required genisoimage
  command_required jq
  command_required openssl
  command_required qemu-img
  command_required qemu-system-x86_64
  command_required sha256sum
  command_required ssh
  command_required ssh-keygen
  command_required ps
  command_required seq
  command_required timeout

  [[ "$OUTPUT_FORMAT" == raw || "$OUTPUT_FORMAT" == qcow2 ]] || die "OMARCHY_OUTPUT_FORMAT must be raw or qcow2"
  [[ "$INSTALL_FIRMWARE" == bios || "$INSTALL_FIRMWARE" == uefi ]] ||
    die "OMARCHY_INSTALL_FIRMWARE must be bios or uefi"
  [[ "$ISO_URL" =~ ^https://[^[:space:]]+\.iso$ ]] || die "OMARCHY_ISO_URL must be an HTTPS .iso URL"
  [[ "$ISO_SHA256_URL" =~ ^https://[^[:space:]]+$ ]] || die "OMARCHY_ISO_SHA256_URL must be an HTTPS URL"
  [[ "$ISO_VERSION" =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "OMARCHY_ISO_VERSION is invalid"
  valid_size "$DISK_SIZE" || die "OMARCHY_DISK_SIZE must be a size such as 50G or 50GiB"
  local disk_size_mib
  disk_size_mib="$(size_in_mib "$DISK_SIZE")"
  ((disk_size_mib >= 32768)) || die "OMARCHY_DISK_SIZE must be at least 32G for Omarchy"

  download_iso
  qemu-img create -f qcow2 "$DISK_FILE" "$DISK_SIZE"
  create_cidata

  local firmware_code="" firmware_vars=""
  if [[ "$INSTALL_FIRMWARE" == uefi ]]; then
    firmware_code="$(find_ovmf_file /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/edk2/x64/OVMF_CODE.fd /usr/share/OVMF/OVMF_CODE.fd)" ||
      die "Unable to find an OVMF firmware code file"
    firmware_vars="$(find_ovmf_file /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/edk2/x64/OVMF_VARS.fd /usr/share/OVMF/OVMF_VARS.fd)" ||
      die "Unable to find an OVMF firmware variables file"
    cp "$firmware_vars" "$WORK_DIR/OVMF_VARS.fd"
    firmware_vars="$WORK_DIR/OVMF_VARS.fd"
    chmod 0600 "$firmware_vars"
  fi

  run_installer_vm "$firmware_code" "$firmware_vars"
  run_configured_guest "$firmware_code" "$firmware_vars"
  configure_guest
  export_artifact
  echo "Omarchy image artifact: $ARTIFACT_FILE"
}

main "$@"
