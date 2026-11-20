#!/bin/bash
# Flash Raspberry Pi OS Lite (32-bit) to an SD card on a Mac, set it up to join
# Wi-Fi and accept your ssh key, and make it install the simple-hifi music player
# (setup.sh) by itself on first boot.
#
#   ./flash-mac.sh            list external disks
#   ./flash-mac.sh disk4      flash /dev/disk4 (asks you to type the disk name to confirm)
#   ./flash-mac.sh --save     copy the music and settings off the running Pi into $BACKUP
#   ./flash-mac.sh --restore  wait for a freshly flashed Pi, then copy the music back onto it
#
# Reflashing a card that already holds a player keeps its music and its paired remote:
#   - If the Pi is reachable over ssh when you flash, the music (/srv/music) and the
#     settings (zigbee2mqtt's network key and paired devices, the web page's job list,
#     where each playlist was left) are saved into $BACKUP first.
#   - The settings go onto the new card; setup.sh puts them back before zigbee2mqtt
#     starts, so the remote stays paired.
#   - The music goes back over ssh: after flashing, this script waits for the new Pi
#     and copies it back by itself (or run ./flash-mac.sh --restore later).
#   Only one SD card? Run ./flash-mac.sh --save while the card is still in the Pi, then
#   take it out and flash it. macOS cannot read the Pi's own (ext4) partition, so a card
#   that is already in the Mac cannot be saved from; the script refuses to erase a player
#   card when there is no backup, unless you set SKIP_SAVE=1.
#
# Needs, next to this script: setup.sh, web/ and wifi.env (WIFI_SSID=..., WIFI_PASS=...).
# Optional: extra-keys.pub, more ssh public keys (one per line) that may log in;
#           cloudflared.token, the Cloudflare Tunnel token for the public web page;
#           local.env, your own names (see local.env.example). All three are git-ignored.
# Optional env: PI_USER (default: your Mac username), PI_HOST (default: local.env, else simple-hifi).
#               PI_KEY: the public key file to use (default: the first of ~/.ssh/id_ed25519.pub,
#               id_ecdsa.pub, id_rsa.pub).
#               PI_SSH: where the Pi answers ssh (default: $PI_USER@$PI_HOST.local).
#               BACKUP: where saved music and settings go (default: ~/$PI_HOST-backup).
#               PI_MUSIC: the music folder on the Pi (default: /srv/music).
#               NO_RESTORE=1: flash a clean player even though $BACKUP holds a saved one.
#
# Written for macOS's bash 3.2: no mapfile, no associative arrays.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
PI_USER=${PI_USER:-$(id -un)}
# Your own names (git-ignored; see local.env.example). The environment wins over the file.
env_pi_host=${PI_HOST:-}
# shellcheck source=/dev/null
if [[ -f $HERE/local.env ]]; then source "$HERE/local.env"; fi
PI_HOST=${env_pi_host:-${PI_HOST:-simple-hifi}}
PI_SSH=${PI_SSH:-$PI_USER@$PI_HOST.local}
PI_MUSIC=${PI_MUSIC:-/srv/music}
BACKUP=${BACKUP:-$HOME/$PI_HOST-backup}
CACHE=${CACHE:-$HOME/Downloads}
# 32-bit: on a Pi 3 the 64-bit kernel drops USB audio frames under load
# (raspberrypi/linux#3795); setup.sh explains.
LATEST=https://downloads.raspberrypi.com/raspios_lite_armhf_latest
# The Pi's state that a reflash would lose, apart from the music (see setup.sh).
STATE_PATHS="var/lib/zigbee2mqtt var/lib/tplayer-web var/lib/private/tplayer"
# Half-finished web downloads; never worth copying.
MUSIC_EXCLUDE=.tplayer-web-tmp/

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

# YAML double-quoted string.
yq() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

pi_ssh() { ssh -o ConnectTimeout=10 -o BatchMode=yes "$PI_SSH" "$@"; }
pi_reachable() { pi_ssh true >/dev/null 2>&1; }

# A saved install exists (and NO_RESTORE is not set).
have_backup() { [[ ${NO_RESTORE:-} != 1 && ( -f $BACKUP/state.tgz || -d $BACKUP/music ) ]]; }
saved_at() { cat "$BACKUP/saved-at" 2>/dev/null || echo "an unknown time"; }

# ---------------------------------------------------------------- save / restore
# Copy the music and settings off the running Pi into $BACKUP.
save_from_pi() {
  local n tries rc
  step "Saving music and settings from $PI_SSH into $BACKUP"
  mkdir -p "$BACKUP"
  chmod 700 "$BACKUP"   # state.tgz holds the Zigbee network key

  # A Pi with no music (say, one just flashed) must not wipe a good backup (--delete).
  n=$(pi_ssh "find '$PI_MUSIC' -type f -not -name '.*' | wc -l") || die "cannot list $PI_MUSIC on $PI_SSH"
  n=$(echo "$n" | tr -d ' ')
  if [[ $n -eq 0 ]]; then
    echo "No music on the Pi; keeping the backup as it is."
  else
    echo "Music: $n files"
    mkdir -p "$BACKUP/music"
    # Low priority on the Pi, so playback does not stutter while it copies.
    rsync -rt --delete --exclude "$MUSIC_EXCLUDE" \
      --rsync-path="nice -n 19 ionice -c3 rsync" \
      -e "ssh -o ConnectTimeout=10 -o BatchMode=yes" \
      "$PI_SSH:$PI_MUSIC/" "$BACKUP/music/" || die "copying the music failed"
  fi

  # tar exits 1 when a file changed while it read it (zigbee2mqtt writes its database
  # now and then): try again, so the copy is a consistent one.
  rc=1
  for tries in 1 2 3; do
    rc=0
    # shellcheck disable=SC2086  # STATE_PATHS is a word list
    (umask 077; pi_ssh "sudo tar -C / -czf - --ignore-failed-read --exclude=var/lib/zigbee2mqtt/log $STATE_PATHS" \
      > "$BACKUP/state.tgz.part") || rc=$?
    [[ $rc -eq 0 ]] && break
    echo "tar exited $rc (try $tries of 3)"
    sleep 2
  done
  [[ $rc -eq 0 ]] || die "saving the settings failed"
  mv "$BACKUP/state.tgz.part" "$BACKUP/state.tgz"
  if tar -tzf "$BACKUP/state.tgz" | grep -qx 'var/lib/zigbee2mqtt/configuration.yaml'; then
    echo "Settings: saved (zigbee2mqtt network key included)"
  else
    echo "WARNING: no zigbee2mqtt configuration was saved; the remote will need pairing again (tplayer-pair)"
  fi
  echo "$(date '+%Y-%m-%d %H:%M') from $PI_SSH" > "$BACKUP/saved-at"
  echo "Saved. ($(du -sh "$BACKUP" | cut -f1) in $BACKUP)"
}

# Wait for a freshly installed Pi (setup.sh finished) and copy the saved music onto it.
# Retries until it works: the first boot reboots once at the end, which can cut a copy short.
restore_to_pi() {
  local deadline
  [[ -d $BACKUP/music ]] || { echo "No saved music in $BACKUP/music; nothing to copy back."; return 0; }
  step "Copying the saved music back to $PI_SSH:$PI_MUSIC (waits for the Pi; Ctrl-C to stop)"
  echo "Waiting for the first-boot install to finish (roughly 10-20 min)..."
  deadline=$(( $(date +%s) + 60 * 60 ))
  while true; do
    if ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$PI_SSH" \
         "test -f /var/lib/cloud/instance/boot-finished -a -d '$PI_MUSIC' -a -w '$PI_MUSIC'" >/dev/null 2>&1 &&
       rsync -rt --exclude "$MUSIC_EXCLUDE" \
         -e "ssh -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
         "$BACKUP/music/" "$PI_SSH:$PI_MUSIC/"; then
      echo "Music copied back. It shows up in the player within a minute."
      return 0
    fi
    if [[ $(date +%s) -ge $deadline ]]; then
      echo "Gave up after an hour. When the Pi is up, run:  ./flash-mac.sh --restore"
      return 1
    fi
    sleep 20
  done
}

case ${1:-} in
  --save)
    pi_reachable || die "cannot reach $PI_SSH over ssh (is the Pi on, with its card in?)"
    save_from_pi
    exit 0
    ;;
  --restore)
    restore_to_pi
    exit $?
    ;;
esac

# ---------------------------------------------------------------- boot files
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

# Reboot once cloud-init has finished, so the audio and kernel settings apply.
power_state:
  mode: reboot
  message: "simple-hifi installed; rebooting"
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
  if [[ -f $HERE/local.env ]]; then cp "$HERE/local.env" "$dir/tplayer/local.env"; fi
  # setup.sh installs it as root-only and deletes this copy on first boot.
  if [[ -f $HERE/cloudflared.token ]]; then cp "$HERE/cloudflared.token" "$dir/tplayer/cloudflared.token"; fi
  cp "$HERE/web/tplayer_web.py" "$dir/tplayer/web/"
  cp "$HERE"/web/static/* "$dir/tplayer/web/static/"
  # The saved settings; setup.sh restores them on first boot, then deletes this copy.
  if have_backup && [[ -f $BACKUP/state.tgz ]]; then
    mkdir -p "$dir/tplayer/restore"
    cp "$BACKUP/state.tgz" "$dir/tplayer/restore/state.tgz"
  fi
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
  echo "Reflashing the player and its card is still in the Pi? Run  $0 --save  first."
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

# ---------------------------------------------------------------- save the old player
# Is this card a player already? Its boot partition (FAT, readable here) holds tplayer/.
player_card=0
old_mp=$(diskutil info "${DISK}s1" 2>/dev/null | sed -n 's/^ *Mount Point: *//p' || true)
if [[ -n $old_mp && -f $old_mp/tplayer/setup.sh ]]; then player_card=1; fi

if pi_reachable; then
  # Another card is running in the Pi (or this one is not the player's): save it now.
  save_from_pi
elif [[ $player_card == 1 ]]; then
  if have_backup; then
    echo
    echo "This card holds a player, and the Pi is not reachable at $PI_SSH."
    echo "Using the backup in $BACKUP, saved $(saved_at)."
  elif [[ ${SKIP_SAVE:-} == 1 ]]; then
    echo "SKIP_SAVE=1: this card's music and paired remote will be lost."
  else
    die "this card holds a player, and its music cannot be read on a Mac. Put it back in the Pi,
       power it on, run  $0 --save , then flash it. (SKIP_SAVE=1 erases it without saving.)"
  fi
elif have_backup; then
  echo
  echo "$BACKUP holds a player saved $(saved_at); it goes onto this card (NO_RESTORE=1 for a clean one)."
fi

echo
read -rp "Everything on $DISK will be erased. Type '$DISK' to continue: " answer
[[ $answer == "$DISK" ]] || die "not confirmed"

# ---------------------------------------------------------------- image
step "Downloading Raspberry Pi OS Lite (32-bit)"
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
if [[ -f $mp/tplayer/restore/state.tgz ]]; then echo "Saved settings are on the card (the remote stays paired)."; fi
sync
diskutil eject "/dev/$DISK"

# The new install has new ssh host keys; forget the old ones so ssh does not refuse it.
ssh-keygen -R "${PI_SSH#*@}" >/dev/null 2>&1 || true

cat <<EOF

Done. The card is ejected.
  1. Put it in the Pi (dongle and speakers can go in now or later).
  2. Power on and leave it: first boot installs everything and reboots once (roughly 10-20 min on a Pi 3; not yet timed).
  3. Then:  ssh $PI_USER@$PI_HOST.local
            tail -f /var/log/tplayer-setup.log      (ends with "Done.")
            tplayer-pair                            (not needed when the settings were restored)
If setup.sh stopped on an error, fix it and run:  sudo OWNER=$PI_USER bash /boot/firmware/tplayer/setup.sh
EOF

if have_backup && [[ -d $BACKUP/music ]]; then
  restore_to_pi || true
fi
