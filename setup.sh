#!/usr/bin/env bash
# the music player: Raspberry Pi 3 + USB speakers + Sonoff ZBDongle-E + IKEA RODRET.
#
# Run once on a fresh Raspberry Pi OS Lite (64-bit), logged in as your normal user.
# The Zigbee dongle and USB speakers can be plugged in at any time, before or after:
#
#     sudo ./setup.sh
#
# Safe to re-run. It never overwrites zigbee2mqtt's configuration.yaml once it exists
# (that file holds the network key, so overwriting it would unpair the remote).
#
# What you get:
#   /srv/music/<name>/  one folder per playlist; drop music in over ssh (rsync).
#                       Each playlist plays in name order and loops; folders rotate in name order.
#   tplayer-pair        opens pairing for 254 s; then press the RODRET's pair button 4 times
#   RODRET top (I)      play / pause; double press = next track
#   RODRET bottom (O)   next playlist, resuming where it was left, playing
#   holds               ignored
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"
# OWNER can be passed in (flash-mac.sh's first-boot run has no SUDO_USER).
OWNER=${OWNER:-${SUDO_USER:-}}
[[ -n $OWNER && $OWNER != root ]] || die "run via sudo from your normal user, not as root"

MUSIC=/srv/music
Z2M=/opt/zigbee2mqtt
Z2M_DATA=/var/lib/zigbee2mqtt
ZIGBEE_CHANNEL=25   # 15, 20 and 25 sit between the common Wi-Fi channels 1, 6 and 11
WIFI_COUNTRY=GB

# ---------------------------------------------------------------- preflight
step "Checking the OS"
[[ $(uname -m) == aarch64 ]] || die "this is $(uname -m); flash the 64-bit Pi OS Lite (zigbee2mqtt is dropping 32-bit)"

# ---------------------------------------------------------------- wi-fi
step "Wi-Fi"
# wifi.env next to this script (WIFI_SSID=..., WIFI_PASS=...) skips the prompts.
# It is git-ignored: keep the password out of any repo.
WIFI_ENV="$(dirname "$(readlink -f "$0")")/wifi.env"
WIFI_SSID="" WIFI_PASS=""
if [[ -f $WIFI_ENV ]]; then
  # shellcheck source=/dev/null
  source "$WIFI_ENV"
  echo "Using Wi-Fi settings from $WIFI_ENV (network: $WIFI_SSID)"
elif [[ -t 0 ]]; then
  read -rp "Wi-Fi network name (blank to skip): " WIFI_SSID
else
  echo "No terminal and no wifi.env: leaving Wi-Fi as it is (cloud-init sets it on a flash-mac.sh card)"
fi
if [[ -n $WIFI_SSID ]]; then
  if [[ -z $WIFI_PASS ]]; then read -rsp "Wi-Fi password: " WIFI_PASS; echo; fi
  [[ -n $WIFI_PASS ]] || die "empty Wi-Fi password"
  raspi-config nonint do_wifi_country "$WIFI_COUNTRY"
  if nmcli -t -f NAME connection show | grep -qxF "tplayer-wifi"; then
    nmcli connection modify tplayer-wifi 802-11-wireless.ssid "$WIFI_SSID" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASS" connection.autoconnect yes
  else
    nmcli connection add type wifi con-name tplayer-wifi ifname wlan0 ssid "$WIFI_SSID" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASS" connection.autoconnect yes
  fi
  unset WIFI_PASS
  # Do not yank an ssh session that is running over wlan0 already.
  if nmcli -t -f DEVICE,STATE device | grep -qx "wlan0:connected"; then
    echo "wlan0 is already connected; the new network applies from the next reboot or reconnect"
  else
    nmcli connection up tplayer-wifi
  fi
fi

# ---------------------------------------------------------------- packages
step "Waiting for the internet"
for _ in $(seq 60); do
  if curl -fsS -o /dev/null --max-time 10 https://deb.debian.org/; then online=1; break; fi
  sleep 5
done
[[ ${online:-0} == 1 ]] || die "no internet after 5 minutes (check Wi-Fi / Ethernet)"

step "Installing packages"
apt-get update
apt-get install -y mpd mpc mosquitto mosquitto-clients jq git curl ca-certificates make g++ gcc libsystemd-dev

node_major=$(node --version 2>/dev/null | sed -n 's/^v\([0-9]*\).*/\1/p' || true)
if [[ $node_major != 22 && $node_major != 24 ]]; then
  step "Installing Node.js 24 (NodeSource)"
  # Pinned to 24: zigbee2mqtt needs 22/24, and Node 25+ no longer ships corepack (pnpm).
  curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/nodesource_setup.sh
  bash /tmp/nodesource_setup.sh
  apt-get install -y nodejs
fi
corepack enable

# ---------------------------------------------------------------- low-write system
step "Keeping SD card writes low"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/tplayer.conf <<'EOF'
# Logs live in RAM: fewer SD writes, so a power cut has less to damage.
[Journal]
Storage=volatile
RuntimeMaxUse=30M
EOF
systemctl restart systemd-journald

# ---------------------------------------------------------------- music folder + mpd
step "Music folder $MUSIC"
mkdir -p "$MUSIC"
chown "$OWNER":audio "$MUSIC"
chmod 2775 "$MUSIC"
# rsync uploads to a hidden temp name and renames when done; never index the temp file.
printf '.*\n' > "$MUSIC/.mpdignore"
chown "$OWNER":audio "$MUSIC/.mpdignore"

step "Reserving ALSA card 0 for USB speakers"
cat > /etc/modprobe.d/tplayer-audio.conf <<'EOF'
# Any USB sound card becomes card 0, whenever it is plugged in; mpd plays to card 0.
options snd slots=snd-usb-audio
options snd-usb-audio index=0
EOF

step "Configuring mpd"
[[ -f /etc/mpd.conf.orig ]] || cp /etc/mpd.conf /etc/mpd.conf.orig
cat > /etc/mpd.conf <<EOF
# Written by simple-hifi setup.sh (original: /etc/mpd.conf.orig)
music_directory    "$MUSIC"
playlist_directory "/var/lib/mpd/playlists"
db_file            "/var/lib/mpd/tag_cache"
state_file         "/var/lib/mpd/state"
user               "mpd"
bind_to_address    "localhost"
log_file           "syslog"
auto_update        "yes"
restore_paused     "yes"
filesystem_charset "UTF-8"

audio_output {
    type   "alsa"
    name   "speakers"
    # Card 0 is reserved for a USB sound card (see /etc/modprobe.d/tplayer-audio.conf).
    device "plughw:0,0"
}
EOF
systemctl enable mpd.service
systemctl restart mpd.service

# ---------------------------------------------------------------- zigbee2mqtt
step "Installing zigbee2mqtt (the build takes a while on a Pi 3)"
systemctl enable --now mosquitto.service
if [[ ! -d $Z2M/.git ]]; then
  mkdir -p "$Z2M"
  chown "$OWNER": "$Z2M"
  sudo -u "$OWNER" git clone --depth 1 https://github.com/Koenkk/zigbee2mqtt.git "$Z2M"
fi
sudo -u "$OWNER" env COREPACK_ENABLE_DOWNLOAD_PROMPT=0 bash -c "cd '$Z2M' && pnpm install --frozen-lockfile && pnpm run prepack"

mkdir -p "$Z2M_DATA"
chown "$OWNER": "$Z2M_DATA"
if [[ -f $Z2M_DATA/configuration.yaml ]]; then
  echo "Keeping existing $Z2M_DATA/configuration.yaml (it holds the network key)"
else
  sudo -u "$OWNER" tee "$Z2M_DATA/configuration.yaml" >/dev/null <<EOF
version: 5
homeassistant:
  enabled: false
frontend:
  enabled: false
mqtt:
  base_topic: zigbee2mqtt
  server: mqtt://localhost
serial:
  # No port: zigbee2mqtt finds the Sonoff dongle by its USB ID when it is plugged in.
  adapter: ember
advanced:
  channel: $ZIGBEE_CHANNEL
  network_key: GENERATE
  pan_id: GENERATE
  ext_pan_id: GENERATE
  log_output:
    - console
EOF
fi
usermod -aG dialout "$OWNER"

cat > /etc/systemd/system/zigbee2mqtt.service <<EOF
[Unit]
Description=zigbee2mqtt
After=network.target mosquitto.service
Requires=mosquitto.service

[Service]
Type=notify
User=$OWNER
SupplementaryGroups=dialout
WorkingDirectory=$Z2M
Environment=NODE_ENV=production
Environment=ZIGBEE2MQTT_DATA=$Z2M_DATA
Environment=Z2M_ONBOARD_NO_SERVER=1
ExecStart=/usr/bin/node index.js
WatchdogSec=10s
Restart=always
RestartSec=10s

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------- player scripts
step "Installing player scripts"
cat > /usr/local/bin/tplayer <<'EOF'
#!/usr/bin/env bash
# simple-hifi player. Each folder directly under the music directory is a playlist.
#   RODRET top (on)     play / pause; double press = next track
#   RODRET bottom (off) next playlist, resuming where it was left, playing
#   holds               ignored
# The queue always holds the current playlist's files in name order, looping.
# shellcheck disable=SC2317  # functions run through with_lock
# Files added to or removed from that folder join or leave the queue live.
set -euo pipefail
export LC_ALL=C

STATE_DIR=${STATE_DIRECTORY:-/var/lib/tplayer}
CURRENT=$STATE_DIR/current
LOCK=$STATE_DIR/lock
POS_DIR=$STATE_DIR/positions
DOUBLE_WINDOW=0.6
mkdir -p "$POS_DIR"

# One playlist change at a time (button loop and sync loop share the queue).
with_lock() { { flock 9 && "$@"; } 9>"$LOCK"; }

# Playlist names: the first path part of every song that sits in a folder.
playlists() { mpc listall | sed -n 's|/.*||p' | sort -u; }

# The saved playlist, or the first one if it is unset or gone.
current_playlist() {
  local all saved=""
  all=$(playlists)
  [[ -f $CURRENT ]] && saved=$(<"$CURRENT")
  if [[ -n $saved ]] && grep -qxF -- "$saved" <<<"$all"; then
    printf '%s\n' "$saved"
  else
    head -n 1 <<<"$all"
  fi
}

sync_queue() {
  mpc -q repeat on
  mpc -q random off
  mpc -q single off
  mpc -q consume off

  local cur f i
  cur=$(current_playlist)
  if [[ -z $cur ]]; then mpc -q clear; return 0; fi
  printf '%s\n' "$cur" > "$CURRENT"

  local -A want=() have=()
  local -a queue=()
  while IFS= read -r f; do
    if [[ -n $f ]]; then want[$f]=1; fi
  done < <(mpc listall "$cur")

  mapfile -t queue < <(mpc -f '%file%' playlist)
  # Remove from the highest position down, so the positions stay valid.
  for (( i = ${#queue[@]} - 1; i >= 0; i-- )); do
    f=${queue[i]}
    if [[ -z ${want[$f]+x} ]]; then
      mpc -q del $(( i + 1 ))
    else
      have[$f]=1
    fi
  done

  # New files go on the end, in name order.
  while IFS= read -r f; do
    if [[ -n $f && -z ${have[$f]+x} ]]; then
      mpc -q add "$f"
    fi
  done < <(mpc listall "$cur" | sort)
}

# Where each playlist was left: one file per playlist, "<song file>\t<seconds>".
pos_path() { printf '%s/%s' "$POS_DIR" "$(printf '%s' "$1" | md5sum | cut -c1-32)"; }

# "1:02:03" or "2:03" -> seconds.
to_seconds() { awk -F: '{ s = 0; for (i = 1; i <= NF; i++) s = s * 60 + $i; print s }' <<<"$1"; }

save_position() {
  local playlist=$1 file elapsed
  file=$(mpc -f '%file%' current)
  if [[ -z $file ]]; then return 0; fi
  # Status line looks like: "[playing] #2/5   0:42/3:10 (22%)"; take the elapsed part.
  elapsed=$(mpc status | sed -n 's|.* \([0-9:][0-9:]*\)/[0-9:][0-9:]* .*|\1|p' | head -n 1)
  printf '%s\t%s\n' "$file" "$(to_seconds "${elapsed:-0}")" > "$(pos_path "$playlist")"
}

# Queue must already hold the playlist. Plays from the saved song and second,
# or from the first song if nothing is saved or the saved song is gone.
resume_playlist() {
  local playlist=$1 saved file="" secs=0 pos=""
  saved=$(pos_path "$playlist")
  if [[ -f $saved ]]; then IFS=$'\t' read -r file secs < "$saved" || true; fi
  if [[ -n $file ]]; then
    pos=$(mpc -f '%file%' playlist | grep -nxF -- "$file" | head -n 1 | cut -d: -f1 || true)
  fi
  if [[ -n $pos ]]; then
    mpc -q play "$pos"
    if [[ ${secs:-0} -gt 0 ]]; then
      mpc -q seek "$(printf '%d:%02d:%02d' $(( secs / 3600 )) $(( secs / 60 % 60 )) $(( secs % 60 )))"
    fi
  else
    mpc -q play 1
  fi
}

next_playlist() {
  local all cur next
  all=$(playlists)
  [[ -n $all ]] || return 0
  cur=$(current_playlist)
  if [[ -n $cur ]]; then save_position "$cur"; fi
  # The line after the current one, wrapping to the first.
  next=$(awk -v cur="$cur" 'found { print; exit } $0 == cur { found = 1 }' <<<"$all")
  [[ -n $next ]] || next=$(head -n 1 <<<"$all")
  printf '%s\n' "$next" > "$CURRENT"
  mpc -q clear
  sync_queue
  resume_playlist "$next"
}

action_of() { jq -r '.action // empty' <<<"$1"; }

# A RODRET double press arrives as two "on" events. So after an "on", wait
# DOUBLE_WINDOW seconds for a second one before treating it as play/pause.
buttons() {
  mosquitto_sub -h localhost -t 'zigbee2mqtt/+' | {
    local msg msg2 action pending=""
    while true; do
      if [[ -n $pending ]]; then
        msg=$pending; pending=""
      else
        IFS= read -r msg || break
      fi
      if ! action=$(action_of "$msg"); then continue; fi
      case $action in
        on)
          if IFS= read -r -t "$DOUBLE_WINDOW" msg2; then
            if [[ $(action_of "$msg2") == on ]]; then
              mpc -q next
              mpc -q play
              continue
            fi
            pending=$msg2
          fi
          mpc -q toggle
          ;;
        off)
          with_lock next_playlist
          ;;
      esac
    done
  }
}

watch_library() {
  mpc idleloop database | while read -r _; do with_lock sync_queue; done
}

until mpc -q status; do sleep 2; done
with_lock sync_queue
watch_library &
buttons &
# If either loop ends, exit so systemd restarts the whole player.
wait -n
exit 1
EOF

cat > /usr/local/bin/tplayer-pair <<'EOF'
#!/usr/bin/env bash
# Open the Zigbee network for 254 s and show join events. Ctrl-C closes it early.
set -euo pipefail
close() { mosquitto_pub -h localhost -t zigbee2mqtt/bridge/request/permit_join -m '{"time": 0}'; }
trap close EXIT
mosquitto_pub -h localhost -t zigbee2mqtt/bridge/request/permit_join -m '{"time": 254}'
echo "Pairing open for 254 s. Take the back cover off the RODRET and press the pair button 4 times."
echo "Wait for 'interview_successful', then press Ctrl-C. Test: top button should print an action."
mosquitto_sub -h localhost -v -W 254 -t zigbee2mqtt/bridge/event -t 'zigbee2mqtt/+' || true
EOF
chmod 755 /usr/local/bin/tplayer /usr/local/bin/tplayer-pair

cat > /etc/systemd/system/tplayer.service <<'EOF'
[Unit]
Description=simple-hifi player (RODRET buttons + playlist folders)
After=mpd.service mosquitto.service
Wants=mpd.service mosquitto.service

[Service]
DynamicUser=yes
StateDirectory=tplayer
ExecStart=/usr/local/bin/tplayer
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now zigbee2mqtt.service tplayer.service

# ---------------------------------------------------------------- done
step "Status"
systemctl --no-pager --lines=0 status mpd mosquitto zigbee2mqtt tplayer || true
cat <<EOF

Done. Plug in the Zigbee dongle and USB speakers if they are not in yet. Next:
  1. Copy music, one folder per playlist:
       rsync -av ~/kids-music/ $OWNER@$(hostname):$MUSIC/
     e.g. $MUSIC/1-nursery/..., $MUSIC/2-greek/...   (playlists rotate in name order)
  2. Pair remote:  tplayer-pair
  3. Test:         top = play/pause (double press = next track), bottom = next playlist.
Logs: journalctl -u zigbee2mqtt -u tplayer -u mpd -f
EOF

# The ALSA card-0 reservation only takes effect after a reboot. Do not reboot from
# here: under cloud-init that can make the first-boot run repeat. flash-mac.sh's
# user-data reboots once cloud-init has finished (power_state).
echo "Reboot once to apply the audio setting (a flash-mac.sh card does this by itself)."
