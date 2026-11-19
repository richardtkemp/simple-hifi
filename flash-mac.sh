#!/bin/bash
# Flash Raspberry Pi OS Lite (64-bit) to an SD card on a Mac, set it up to join
# Wi-Fi and accept your ssh key, and make it install the music player
# (setup.sh) by itself on first boot.
#
#   ./flash-mac.sh            list external disks
#   ./flash-mac.sh disk4      flash /dev/disk4 (asks you to type the disk name to confirm)
#
# Needs, next to this script: setup.sh, web/ and wifi.env (WIFI_SSID=..., WIFI_PASS=...).
# Optional: extra-keys.pub, more ssh public keys (one per line) that may log in.
# Optional env: PI_USER (default: your Mac username), PI_HOST (default: simple-hifi).
#               PI_KEY: the public key file to use (default: the first of ~/.ssh/id_ed25519.pub,
#               id_ecdsa.pub, id_rsa.pub).
#
# Written for macOS's bash 3.2: no mapfile, no associative arrays.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PI_USER=${PI_USER:-$(id -un)}
PI_HOST=${PI_HOST:-simple-hifi}
CACHE=${CACHE:-$HOME/Downloads}
LATEST=https://downloads.raspberrypi.com/raspios_lite_arm64_latest

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# YAML double-quoted string.
yq() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

# The authorized_keys entries for user-data: your Mac key, plus each line of
# extra-keys.pub next to this script (optional; blank lines and # comments skipped).
key_lines() {
  echo "      - $(yq "$PUBKEY")"
  [[ -f $HERE/extra-keys.pub ]] || return 0
  local line
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] && continue
    echo "      - $(yq "$line")"
  done < "$HERE/extra-keys.pub"
}

# Write the cloud-init files and setup.sh into a mounted boot partition.
write_bootfs() {
  local dir=$1
  cat > "$dir/user-data" <<EOF
#cloud-config
hostname: $PI_HOST
manage_etc_hosts: true
timezone: Europe/London

users:
  - name: $PI_USER
    groups: users,adm,dialout,audio,netdev,video,plugdev,sudo
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys:
$(key_lines)
    sudo: ALL=(ALL) NOPASSWD:ALL

enable_ssh: true
ssh_pwauth: false

runcmd:
  - [bash, -c, "OWNER=$PI_USER bash /boot/firmware/tplayer/setup.sh > /var/log/tplayer-setup.log 2>&1"]

# Reboot once cloud-init has finished, so the audio setting applies.
power_state:
  mode: reboot
  message: "simple-hifi player installed; rebooting"
  condition: true
EOF

  cat > "$dir/network-config" <<EOF
network:
  version: 2
  wifis:
    renderer: NetworkManager
    wlan0:
      dhcp4: true
      regulatory-domain: "GB"
      access-points:
        $(yq "$WIFI_SSID"):
          password: $(yq "$WIFI_PASS")
      optional: true
EOF

  mkdir -p "$dir/tplayer/web/static"
  cp "$HERE/setup.sh" "$dir/tplayer/setup.sh"
  cp "$HERE/web/tplayer_web.py" "$dir/tplayer/web/"
  cp "$HERE"/web/static/* "$dir/tplayer/web/static/"
}

# ---------------------------------------------------------------- inputs
[[ -f $HERE/setup.sh ]] || die "setup.sh not found next to this script"
[[ -f $HERE/web/tplayer_web.py ]] || die "web/ not found next to this script"
[[ -f $HERE/wifi.env ]] || die "wifi.env not found next to this script"
WIFI_SSID="" WIFI_PASS=""
# shellcheck source=/dev/null
source "$HERE/wifi.env"
[[ -n $WIFI_SSID && -n $WIFI_PASS ]] || die "wifi.env must set WIFI_SSID and WIFI_PASS"

PUBKEY=""
if [[ -n ${PI_KEY:-} ]]; then
  [[ -f $PI_KEY ]] || die "PI_KEY: no such file: $PI_KEY"
  PUBKEY=$(cat "$PI_KEY")
fi
[[ -n $PUBKEY ]] || for k in id_ed25519.pub id_ecdsa.pub id_rsa.pub; do
  if [[ -f $HOME/.ssh/$k ]]; then PUBKEY=$(cat "$HOME/.ssh/$k"); break; fi
done
[[ -n $PUBKEY ]] || die "no ssh public key in ~/.ssh (id_ed25519.pub / id_ecdsa.pub / id_rsa.pub)"

# Test hook: write the boot files into a plain directory, touch no disk.
if [[ ${1:-} == --bootfs-only ]]; then
  [[ -d ${2:-} ]] || die "usage: $0 --bootfs-only <existing dir>"
  write_bootfs "$2"
  echo "wrote boot files into $2"
  exit 0
fi

[[ $(uname) == Darwin ]] || die "this script is for macOS"

if [[ $# -ne 1 ]]; then
  diskutil list external physical
  echo
  echo "usage: $0 diskN    (pick the SD card from the list above)"
  exit 1
fi

DISK=${1#/dev/}
[[ $DISK =~ ^disk[0-9]+$ ]] || die "give a whole disk like disk4, not '$1'"

# ---------------------------------------------------------------- disk safety
info=$(diskutil info "$DISK") || die "no such disk: $DISK"
boot_whole=$(diskutil info / | sed -n 's/^ *Part of Whole: *//p')
[[ $DISK != "$boot_whole" ]] || die "$DISK holds macOS"
if ! grep -Eq 'Device Location: +External|Removable Media: +Removable' <<<"$info"; then
  die "$DISK is not external or removable; refusing"
fi
echo "$info" | grep -E 'Device / Media Name|Disk Size|Device Location|Removable Media|Protocol' || true
echo
read -rp "Everything on $DISK will be erased. Type '$DISK' to continue: " answer
[[ $answer == "$DISK" ]] || die "not confirmed"

# ---------------------------------------------------------------- image
step "Downloading Raspberry Pi OS Lite (64-bit)"
url=$(curl -fsSIL -o /dev/null -w '%{url_effective}' "$LATEST")
img="$CACHE/$(basename "$url")"
echo "$url"
if [[ ! -f $img ]]; then
  curl -fL -o "$img.part" "$url"
  mv "$img.part" "$img"
fi
curl -fsSL -o "$img.sha256" "$url.sha256"
(cd "$CACHE" && shasum -a 256 -c "$(basename "$img").sha256") || die "checksum mismatch: delete $img and run again"

if command -v xz >/dev/null; then
  decompress() { xz -dc "$img"; }
else
  decompress() { python3 -c 'import lzma,shutil,sys; shutil.copyfileobj(lzma.open(sys.argv[1]), sys.stdout.buffer, 4 << 20)' "$img"; }
fi

step "Writing to /dev/$DISK (a few minutes; sudo asks for your Mac password)"
diskutil unmountDisk "/dev/$DISK"
decompress | sudo dd of="/dev/r$DISK" bs=4m
sync

# ---------------------------------------------------------------- first-boot config
step "Adding Wi-Fi, ssh key and the player setup"
diskutil mountDisk "/dev/$DISK" || true
mp=""
for _ in $(seq 30); do
  mp=$(diskutil info "${DISK}s1" | sed -n 's/^ *Mount Point: *//p')
  [[ -n $mp && -d $mp ]] && break
  sleep 1
done
[[ -n $mp && -d $mp ]] || die "boot partition ${DISK}s1 did not mount"
[[ -f $mp/config.txt && -f $mp/user-data ]] || die "$mp does not look like a Pi OS boot partition with cloud-init"
write_bootfs "$mp"
sync
diskutil eject "/dev/$DISK"

cat <<EOF

Done. The card is ejected.
  1. Put it in the Pi (dongle and speakers can go in now or later).
  2. Power on and leave it: first boot installs everything and reboots once (roughly 10-20 min on a Pi 3; not yet timed).
  3. Then:  ssh $PI_USER@$PI_HOST.local
            tail -f /var/log/tplayer-setup.log      (ends with "Done.")
            tplayer-pair
If setup.sh stopped on an error, fix it and run:  sudo OWNER=$PI_USER bash /boot/firmware/tplayer/setup.sh
EOF
