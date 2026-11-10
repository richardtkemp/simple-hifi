#!/usr/bin/env bash
# the music player: Raspberry Pi 3 + USB speakers + Sonoff ZBDongle-E + IKEA RODRET.
#
# Run once on a fresh Raspberry Pi OS Lite (64-bit), logged in as your normal user,
# with the USB speakers and the Zigbee dongle plugged in:
#
#     sudo ./setup.sh
#
# Safe to re-run. It never overwrites zigbee2mqtt's configuration.yaml once it exists
# (that file holds the network key, so overwriting it would unpair the remote).
#
# What you get:
#   /srv/music          drop music here over ssh (rsync); it plays in name order, looping
#   tplayer-pair        opens pairing for 254 s; then press the RODRET's pair button 4 times
#   RODRET top (I)      play / pause
#   RODRET bottom (O)   next track (and starts playing if paused)
#   holds               ignored
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"
OWNER=${SUDO_USER:-}
[[ -n $OWNER && $OWNER != root ]] || die "run via sudo from your normal user, not as root"

MUSIC=/srv/music
Z2M=/opt/zigbee2mqtt
Z2M_DATA=/var/lib/zigbee2mqtt
ZIGBEE_CHANNEL=25   # 15, 20 and 25 sit between the common Wi-Fi channels 1, 6 and 11
WIFI_COUNTRY=GB

# ---------------------------------------------------------------- preflight
step "Checking hardware"
[[ $(uname -m) == aarch64 ]] || die "this is $(uname -m); flash the 64-bit Pi OS Lite (zigbee2mqtt is dropping 32-bit)"

shopt -s nullglob
dongles=(/dev/serial/by-id/*)
shopt -u nullglob
[[ ${#dongles[@]} -eq 1 ]] || die "expected exactly 1 serial device in /dev/serial/by-id, found ${#dongles[@]}: plug in the Zigbee dongle (and only it)"
DONGLE=${dongles[0]}
echo "Zigbee dongle: $DONGLE"

mapfile -t cards < <(sed -n 's/^ *[0-9]\+ \[\([^] ]*\) *\]: USB-Audio.*/\1/p' /proc/asound/cards)
[[ ${#cards[@]} -eq 1 ]] || die "expected exactly 1 USB sound card, found ${#cards[@]}: plug in the USB speakers (see /proc/asound/cards)"
CARD=${cards[0]}
echo "USB speakers: ALSA card '$CARD'"

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
else
  read -rp "Wi-Fi network name (blank to skip): " WIFI_SSID
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
    device "plughw:CARD=$CARD,DEV=0"
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
  port: $DONGLE
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
cat > /usr/local/bin/tplayer-sync <<'EOF'
#!/usr/bin/env bash
# Keep mpd's queue equal to the music folder: add new files, drop removed ones.
# Runs once at start, then again after every mpd database update.
set -euo pipefail
export LC_ALL=C

sync_queue() {
  mpc -q repeat on
  mpc -q random off
  mpc -q single off
  mpc -q consume off

  local -A in_db=() in_queue=()
  local -a queue=()
  local f i
  while IFS= read -r f; do
    if [[ -n $f ]]; then in_db[$f]=1; fi
  done < <(mpc listall)

  mapfile -t queue < <(mpc -f '%file%' playlist)
  # Remove from the highest position down, so the positions stay valid.
  for (( i = ${#queue[@]} - 1; i >= 0; i-- )); do
    f=${queue[i]}
    if [[ -z ${in_db[$f]+x} ]]; then
      mpc -q del $(( i + 1 ))
    else
      in_queue[$f]=1
    fi
  done

  # New files go on the end, in name order.
  while IFS= read -r f; do
    if [[ -n $f && -z ${in_queue[$f]+x} ]]; then
      mpc -q add "$f"
    fi
  done < <(mpc listall | sort)
}

until mpc -q status; do sleep 2; done
sync_queue
mpc idleloop database | while read -r _; do sync_queue; done
EOF

cat > /usr/local/bin/tplayer-buttons <<'EOF'
#!/usr/bin/env bash
# RODRET top (on) = play/pause, bottom (off) = next. Holds (brightness_*) are ignored.
set -euo pipefail
mosquitto_sub -h localhost -t 'zigbee2mqtt/+' | while IFS= read -r msg; do
  if ! action=$(jq -r '.action // empty' <<<"$msg"); then continue; fi
  case $action in
    on)  mpc -q toggle ;;
    off) mpc -q next; mpc -q play ;;
  esac
done
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
chmod 755 /usr/local/bin/tplayer-sync /usr/local/bin/tplayer-buttons /usr/local/bin/tplayer-pair

for svc in sync buttons; do
  cat > /etc/systemd/system/tplayer-$svc.service <<EOF
[Unit]
Description=simple-hifi player: $svc
After=mpd.service mosquitto.service
Wants=mpd.service mosquitto.service

[Service]
DynamicUser=yes
ExecStart=/usr/local/bin/tplayer-$svc
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
done

systemctl daemon-reload
systemctl enable --now zigbee2mqtt.service tplayer-sync.service tplayer-buttons.service

# ---------------------------------------------------------------- done
step "Status"
systemctl --no-pager --lines=0 status mpd mosquitto zigbee2mqtt tplayer-sync tplayer-buttons || true
cat <<EOF

Done. Next:
  1. Copy music:   rsync -av ~/kids-music/ $OWNER@$(hostname):$MUSIC/
  2. Pair remote:  tplayer-pair
  3. Test:         press the RODRET top button.
Logs: journalctl -u zigbee2mqtt -u tplayer-buttons -u tplayer-sync -u mpd -f
EOF
