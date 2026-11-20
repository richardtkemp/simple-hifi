#!/usr/bin/env bash
# simple-hifi: a music player on a Raspberry Pi 3 + USB speakers + Sonoff ZBDongle-E + IKEA RODRET.
#
# Run once on a fresh Raspberry Pi OS Lite (32-bit), logged in as your normal user.
# 32-bit, because a Pi 3's USB driver loses its fast interrupt (FIQ) under the 64-bit
# kernel, and the USB speakers then go silent under load (raspberrypi/linux#3795).
# The Zigbee dongle and USB speakers can be plugged in at any time, before or after:
#
#     sudo ./setup.sh
#
# Safe to re-run. It never overwrites zigbee2mqtt's configuration.yaml once it exists
# (that file holds the network key, so overwriting it would unpair the remote).
#
# restore/state.tgz next to this script (flash-mac.sh puts it there when it reflashes a
# player) brings back the old install's zigbee2mqtt data, web job list and playlist
# positions, so the remote stays paired. It is used only on a fresh install, then deleted.
#
# What you get:
#   /srv/music/<name>/  one folder per playlist; drop music in over ssh (rsync).
#                       Each playlist plays in name order and loops; folders rotate in name order.
#   tplayer-pair        opens pairing for 254 s; then press the RODRET's pair button 4 times
#   RODRET top (I)      play / pause; double press = next track
#   RODRET bottom (O)   next playlist, resuming where it was left, playing
#   holds               ignored
#   http://<pi>:8090/   web page: paste YouTube links (or share them from the YouTube app)
#                       and pick an album; the Pi downloads the audio into it (web/)
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }
step() { echo; echo "==> $*"; }

[[ $EUID -eq 0 ]] || die "run with sudo: sudo $0"
# OWNER can be passed in (flash-mac.sh's first-boot run has no SUDO_USER).
OWNER=${OWNER:-${SUDO_USER:-}}
[[ -n $OWNER && $OWNER != root ]] || die "run via sudo from your normal user, not as root"

MUSIC=/srv/music
Z2M=/opt/zigbee2mqtt
# Prebuilt npm release (no compile on the Pi). Tested: 2.14.3 installs and starts with no build.
Z2M_VERSION=2.14.3
Z2M_DATA=/var/lib/zigbee2mqtt
ZIGBEE_CHANNEL=25   # 15, 20 and 25 sit between the common Wi-Fi channels 1, 6 and 11
WIFI_COUNTRY=GB
WEB_PORT=8090
WEB_LIB=/usr/local/lib/tplayer-web
YTDLP=/usr/local/bin/yt-dlp
# Cloudflare Tunnel token for the public web page. A secret, never in this repo: it comes from
# cloudflared.token next to this script (git-ignored; flash-mac.sh puts it on the card), or is put
# there by hand (root, mode 600). No token = no tunnel, and that step is skipped.
CF_TOKEN=/etc/cloudflared/tplayer.token
HERE=$(dirname "$(readlink -f "$0")")
# Your own names (git-ignored; see local.env.example): PLAYER_TITLE, CONTACT_NAME, PUBLIC_HOST.
PLAYER_TITLE="" CONTACT_NAME="" PUBLIC_HOST=""
# shellcheck source=/dev/null
if [[ -f $HERE/local.env ]]; then source "$HERE/local.env"; fi
PLAYER_TITLE=${PLAYER_TITLE:-Music}
RESTORE=$HERE/restore/state.tgz
TPLAYER_LOG=/var/log/tplayer.log
# Node.js on 32-bit (armhf): NodeSource's armhf packages stopped at 22.15, and Node 24 has
# no armv7 build, so the official nodejs.org 22.x build goes into /usr/local.
# zigbee2mqtt 2.14.3 needs ^22.2 and yt-dlp's node runtime needs 22+. Node 22 is
# supported until April 2027; after that there is no newer Node for 32-bit ARM.
NODE22_DIST=https://nodejs.org/dist/latest-v22.x

# ---------------------------------------------------------------- preflight
step "Checking the OS"
ARCH=$(dpkg --print-architecture)
case $ARCH in
  armhf) ;;
  arm64) echo "64-bit Pi OS: works, but a Pi 3's USB audio needs dwc_otg.speed=1 (set below); 32-bit is the tested setup" ;;
  *) die "this is $ARCH; flash the 32-bit Raspberry Pi OS Lite (armhf)" ;;
esac
if [[ $ARCH == armhf && $(uname -m) == aarch64 ]]; then
  echo "WARNING: 64-bit kernel under 32-bit Pi OS (arm_64bit=1 in config.txt?): the Pi 3 USB fix needs the 32-bit kernel"
fi
[[ -f $HERE/web/tplayer_web.py ]] || die "web/ is missing next to setup.sh (copy the whole simple-hifi folder)"
RESTORE_LIST=""
if [[ -f $RESTORE ]]; then
  RESTORE_LIST=$(tar -tzf "$RESTORE") || die "$RESTORE is not a readable tar.gz"
fi
# True when the restore archive holds the directory $1 (e.g. var/lib/zigbee2mqtt).
restorable() { grep -q "^$1/" <<<"$RESTORE_LIST"; }

# ---------------------------------------------------------------- wi-fi
step "Wi-Fi"
# wifi.env next to this script (WIFI_SSID=..., WIFI_PASS=...) skips the prompts.
# It is git-ignored: keep the password out of any repo.
WIFI_ENV="$HERE/wifi.env"
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
# python3 + ffmpeg: the web page and yt-dlp (yt-dlp itself comes from upstream below).
# rsync: music goes in, and flash-mac.sh saves and restores it, over ssh. xz-utils: Node.js on 32-bit.
apt-get install -y mpd mpc mosquitto mosquitto-clients jq curl ca-certificates python3 ffmpeg rsync xz-utils

# Debian's own nodejs (20) is too old: zigbee2mqtt needs 22 or 24.
node_version=$(node --version 2>/dev/null || true)
if [[ $ARCH == armhf ]]; then
  # The newest 22.x armv7l build; re-running setup.sh updates it.
  node_tar=$(curl -fsSL "$NODE22_DIST/SHASUMS256.txt" | awk '/ node-v22\.[0-9.]*-linux-armv7l\.tar\.xz$/ { print $2; exit }' || true)
  if [[ -z $node_tar ]]; then
    [[ -n $node_version ]] || die "cannot find the Node.js 22 armv7l build at $NODE22_DIST"
    echo "Cannot reach $NODE22_DIST; keeping Node.js $node_version"
  elif [[ $node_tar != "node-$node_version-linux-armv7l.tar.xz" ]]; then
    step "Installing ${node_tar%-linux-*} (nodejs.org armv7l build) into /usr/local"
    curl -fsSL -o "/tmp/$node_tar" "$NODE22_DIST/$node_tar"
    (cd /tmp && curl -fsSL "$NODE22_DIST/SHASUMS256.txt" | grep -F " $node_tar" | sha256sum -c -) \
      || die "checksum mismatch for $node_tar"
    tar -xJf "/tmp/$node_tar" -C /usr/local --strip-components=1 --no-same-owner \
      --exclude=CHANGELOG.md --exclude=LICENSE --exclude=README.md
    rm -f "/tmp/$node_tar"
  fi
else
  node_major=$(sed -n 's/^v\([0-9]*\).*/\1/p' <<<"$node_version")
  if [[ $node_major != 22 && $node_major != 24 ]]; then
    step "Installing Node.js 24 (NodeSource)"
    curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/nodesource_setup.sh
    bash /tmp/nodesource_setup.sh
    apt-get install -y nodejs
  fi
fi
NODE=$(command -v node) || die "node is not installed"
echo "Node.js $("$NODE" --version) at $NODE"

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

# The player's own event log (button presses, track changes, unplayable files): small, and
# kept across reboots, unlike the journal. root:adm 0664: the player (adm group, see its
# unit) appends, people in adm read it. Never truncated here.
[[ -f $TPLAYER_LOG ]] || install -m 664 -o root -g adm /dev/null "$TPLAYER_LOG"
chown root:adm "$TPLAYER_LOG"
chmod 664 "$TPLAYER_LOG"

# ---------------------------------------------------------------- kernel command line
step "Kernel command line (USB audio, memory cgroup)"
# dwc_otg.speed=1: USB runs at full speed (12 Mbit/s), so the Pi 3's USB driver does no
#   split transactions. Without it the USB speakers went silent under load (a download,
#   memory copying) while mpd kept playing; with it they held (19 Nov 2026). Full speed is
#   enough for the speakers and the Zigbee dongle; the Pi 3's Ethernet (on the same USB) is
#   then slow, and it is unused.
# cgroup_enable=memory: Pi OS turns the memory cgroup off by default; mpd's MemoryMin
#   and the web page's MemoryHigh/MemoryMax (below) need it.
# Both apply from the next reboot.
CMDLINE=/boot/firmware/cmdline.txt
if [[ -f $CMDLINE ]]; then
  for opt in cgroup_enable=memory dwc_otg.speed=1; do
    if ! grep -qE "(^| )$opt( |\$)" "$CMDLINE"; then
      sed -i "1s/[[:space:]]*\$/ $opt/" "$CMDLINE"
      echo "added $opt"
    fi
  done
else
  echo "WARNING: no $CMDLINE; add cgroup_enable=memory dwc_otg.speed=1 to the kernel command line by hand"
fi

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
options snd-usb-audio index=0 lowlatency=0
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
    # 2 s of sound queued in the speakers' driver: a memory-pressure stall (yt-dlp) must not leave a gap,
    # because these speakers stay silent after a gap until the stream restarts.
    buffer_time "2000000"
}
EOF
mkdir -p /etc/systemd/system/mpd.service.d
cat > /etc/systemd/system/mpd.service.d/memory.conf <<'EOF'
[Service]
# Keep mpd's memory (and its read-ahead of the song) out of reach of a download's memory squeeze:
# a stall leaves a gap, and these USB speakers stay silent after a gap. Needs cgroup_enable=memory.
MemoryMin=160M
MemorySwapMax=0
EOF
systemctl daemon-reload
systemctl enable mpd.service
systemctl restart mpd.service

# ---------------------------------------------------------------- zigbee2mqtt
step "Installing zigbee2mqtt $Z2M_VERSION (prebuilt npm package)"
systemctl enable --now mosquitto.service
mkdir -p "$Z2M"
chown "$OWNER": "$Z2M"
# --omit=optional skips unix-dgram, the one dependency that would need a compiler
# (it only provides systemd notify, so the unit below is Type=simple).
sudo -H -u "$OWNER" bash -c "cd '$Z2M' && npm install --omit=optional --no-audit --no-fund 'zigbee2mqtt@$Z2M_VERSION'"

mkdir -p "$Z2M_DATA"
chown "$OWNER": "$Z2M_DATA"
if restorable var/lib/zigbee2mqtt && [[ ! -f $Z2M_DATA/configuration.yaml ]]; then
  echo "Restoring zigbee2mqtt's data (network key, paired remote) from $RESTORE"
  tar -xzf "$RESTORE" -C "$Z2M_DATA" --strip-components=3 --no-same-owner var/lib/zigbee2mqtt
  chown -R "$OWNER": "$Z2M_DATA"
fi
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
Type=simple
User=$OWNER
SupplementaryGroups=dialout
WorkingDirectory=$Z2M/node_modules/zigbee2mqtt
Environment=NODE_ENV=production
Environment=ZIGBEE2MQTT_DATA=$Z2M_DATA
Environment=Z2M_ONBOARD_NO_SERVER=1
ExecStart=$NODE index.js
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
# User-visible events (button presses, track changes, tracks gained or lost)
# are logged to /var/log/tplayer.log, one line each. The file is root:adm
# 0664 and the unit runs with the adm supplementary group, so both the
# dynamic service user and humans in adm can read (and the service append).
# mpd decode failures are relayed from the journal into the same log.
set -euo pipefail
export LC_ALL=C

STATE_DIR=${STATE_DIRECTORY:-/var/lib/tplayer}
CURRENT=$STATE_DIR/current
LOCK=$STATE_DIR/lock
POS_DIR=$STATE_DIR/positions
LOG=/var/log/tplayer.log
DOUBLE_WINDOW=0.6
mkdir -p "$POS_DIR"

# One log line: "2026-11-19 18:38:01 <message>".
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

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

# Rebuild the queue from the music folder. With the argument "switch" (playlist
# change or startup) the per-track lines are suppressed: the switch itself is
# logged by the caller, and re-listing every track would only repeat it.
sync_queue() {
  local quiet=${1:-} cur f i
  mpc -q repeat on
  mpc -q random off
  mpc -q single off
  mpc -q consume off

  cur=$(current_playlist)
  if [[ -z $cur ]]; then
    mpc -q clear
    log "playlist: none (no music found)"
    return 0
  fi
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
      if [[ -z $quiet ]]; then log "lost: $f"; fi
    else
      have[$f]=1
    fi
  done

  # New files go on the end, in name order.
  while IFS= read -r f; do
    if [[ -n $f && -z ${have[$f]+x} ]]; then
      mpc -q add "$f"
      if [[ -z $quiet ]]; then log "found: $f"; fi
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

# Advance one track: wraps to the first after the last, and restarts the
# song from its beginning when it is the queue's only one. mpc next cannot
# be used here: on a single-song queue (and past the end without repeat)
# MPD stops playback outright, so the target position is computed and
# played directly.
next_track() {
  local pos len
  # mpc's status line, e.g. "[playing] #3/7" or "[paused]  #1/1" (two spaces).
  read -r pos len <<<"$(mpc status | sed -n 's/^\[[a-z]*\]  *#\([0-9]\+\)\/\([0-9]\+\).*/\1 \2/p')"
  if [[ -n $len ]]; then
    mpc -q play $(( pos % len + 1 )) || true
  else
    mpc -q play || true
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
  # The bottom button is the only caller, so log the press with its effect.
  log "button bottom: playlist '$cur' -> '$next'"
  printf '%s\n' "$next" > "$CURRENT"
  mpc -q clear
  sync_queue switch
  resume_playlist "$next"
}

action_of() { jq -r '.action // empty' <<<"$1"; }

# A RODRET double press arrives as two "on" events. So after an "on", wait
# DOUBLE_WINDOW seconds for a second one before treating it as play/pause.
buttons() {
  mosquitto_sub -h localhost -t 'zigbee2mqtt/+' | {
    local msg msg2 action state pending=""
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
              next_track
              log "button top x2: next track"
              continue
            fi
            pending=$msg2
          fi
          # mpc toggle answers with a status line; keep just the new state.
          state=$(mpc toggle | sed -n 's/^\[\([a-z]*\)\].*/\1/p' || true)
          log "button top: ${state:-toggle}"
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

# Log whenever the track actually changes (button skip, playlist switch, or a
# song ending). Pause and resume of the same track stay quiet.
watch_player() {
  local last="" song
  mpc idleloop player | while read -r _; do
    song=$(mpc -f '%file%' current)
    if [[ -n $song && $song != "$last" ]]; then
      log "track: $song"
    fi
    last=$song
  done
}

# Relay mpd's own complaints from the journal: when a file is unplayable mpd
# logs "decoder: Failed to decode: <file>" and skips to the next song by
# itself; this records which file. The outer loop restarts the journal follow
# if it ever drops, so a broken relay cannot take the player down with it.
watch_mpd() {
  local line
  while true; do
    while IFS= read -r line; do
      case $line in *"Failed to decode"*) log "mpd: $line";; esac
    done < <(journalctl -u mpd -f -n 0 -o cat 2>/dev/null)
    sleep 5
  done
}

until mpc -q status; do sleep 2; done
log "player: started, playlist '$(current_playlist)'"
with_lock sync_queue switch
watch_library &
watch_player &
watch_mpd &
buttons &
# If any loop ends, exit so systemd restarts the whole player.
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

# Where each playlist was left. DynamicUser keeps it in /var/lib/private/tplayer
# (/var/lib/tplayer links there); systemd gives it to the service user at start.
if restorable var/lib/private/tplayer && [[ ! -e /var/lib/private/tplayer ]]; then
  echo "Restoring playlist positions from $RESTORE"
  install -d -m 700 /var/lib/private
  tar -xzf "$RESTORE" -C / --no-same-owner var/lib/private/tplayer
fi

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

# The event log (see the top of tplayer). The dynamic user's file system is read-only
# apart from its state directory, hence ReadWritePaths; systemd-journal lets it follow
# mpd's journal for decode failures.
mkdir -p /etc/systemd/system/tplayer.service.d
cat > /etc/systemd/system/tplayer.service.d/log.conf <<EOF
[Service]
SupplementaryGroups=systemd-journal adm
ReadWritePaths=$TPLAYER_LOG
EOF

# ---------------------------------------------------------------- web page for adding music
step "yt-dlp (official build, updated daily)"
# Debian's yt-dlp falls behind YouTube within weeks. The official zipapp runs on the
# system python, bundles the YouTube challenge scripts (yt-dlp-ejs), and updates itself.
# Its JavaScript runtime is the Node.js installed above (tplayer-web passes --js-runtimes node).
if [[ -x $YTDLP ]]; then
  "$YTDLP" -U || echo "yt-dlp update failed; keeping the installed $("$YTDLP" --version)"
else
  curl -fsSL -o "$YTDLP.part" https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp
  chmod 755 "$YTDLP.part"
  mv "$YTDLP.part" "$YTDLP"
fi
echo "yt-dlp $("$YTDLP" --version)"

cat > /etc/systemd/system/tplayer-ytdlp-update.service <<EOF
[Unit]
Description=Update yt-dlp (YouTube changes often)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$YTDLP -U
EOF

cat > /etc/systemd/system/tplayer-ytdlp-update.timer <<'EOF'
[Unit]
Description=Update yt-dlp daily

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

step "Web page for adding YouTube music (port $WEB_PORT)"
install -d -m 755 "$WEB_LIB" "$WEB_LIB/static"
install -m 644 "$HERE/web/tplayer_web.py" "$WEB_LIB/"
install -m 644 "$HERE"/web/static/* "$WEB_LIB/static/"
if restorable var/lib/tplayer-web && [[ ! -e /var/lib/tplayer-web/jobs.json ]]; then
  echo "Restoring the web page's job list from $RESTORE"
  install -d -m 755 -o "$OWNER" -g audio /var/lib/tplayer-web
  tar -xzf "$RESTORE" -C / --no-same-owner var/lib/tplayer-web
  chown -R "$OWNER":audio /var/lib/tplayer-web
fi

cat > /etc/systemd/system/tplayer-web.service <<EOF
[Unit]
Description=simple-hifi web page (add YouTube music to an album)
After=network-online.target
Wants=network-online.target

[Service]
# Runs as the music folder's owner, so new files are owned like hand-copied ones.
User=$OWNER
Group=audio
UMask=0002
StateDirectory=tplayer-web
Environment=TPLAYER_MUSIC=$MUSIC
Environment=TPLAYER_PORT=$WEB_PORT
Environment=TPLAYER_YTDLP=$YTDLP
Environment="TPLAYER_TITLE=${PLAYER_TITLE//%/%%}"
Environment="TPLAYER_CONTACT=${CONTACT_NAME//%/%%}"
ExecStart=/usr/bin/python3 $WEB_LIB/tplayer_web.py
Nice=10
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
mkdir -p /etc/systemd/system/tplayer-web.service.d
cat > /etc/systemd/system/tplayer-web.service.d/low-priority.conf <<'EOF'
[Service]
# Downloads at idle priority on cores 1-3, so mpd and the USB speakers always have CPU and disk.
Nice=19
IOSchedulingClass=idle
CPUWeight=20
AllowedCPUs=1-3

# A download (yt-dlp + Node, ~300-400 MB) reclaims its own memory above this, not mpd's.
MemoryHigh=400M
MemoryMax=650M
EOF

# ---------------------------------------------------------------- public URL (Cloudflare Tunnel)
if [[ -f $HERE/cloudflared.token ]]; then
  echo "Installing the tunnel token from $HERE/cloudflared.token"
  install -D -m 600 -o root -g root "$HERE/cloudflared.token" "$CF_TOKEN"
  # The card's boot partition is readable by anyone holding the card: no copy stays there.
  if [[ $HERE == /boot/* ]]; then rm -f "$HERE/cloudflared.token"; fi
fi
# The tunnel runs on the Pi, so no static IP is needed. Cloudflare Access (login) and the
# tunnel's route to http://localhost:$WEB_PORT are set up in Cloudflare, not here.
if [[ -f $CF_TOKEN ]]; then
  step "Cloudflare Tunnel (cloudflared)"
  if ! command -v cloudflared >/dev/null; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /usr/share/keyrings/cloudflare-main.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
      > /etc/apt/sources.list.d/cloudflared.list
    apt-get update
    apt-get install -y cloudflared
  fi
  cat > /etc/systemd/system/cloudflared-tplayer.service <<EOF
[Unit]
Description=Cloudflare Tunnel for ${PUBLIC_HOST:-the web page}
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run --token-file $CF_TOKEN
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
fi

systemctl daemon-reload
systemctl enable --now zigbee2mqtt.service tplayer.service tplayer-ytdlp-update.timer
if [[ -f $CF_TOKEN ]]; then systemctl enable --now cloudflared-tplayer.service; fi
systemctl enable tplayer-web.service
systemctl restart tplayer-web.service   # picks up a new tplayer_web.py on a re-run

# The archive holds the Zigbee network key: gone once used (or skipped on a re-run).
if [[ -f $RESTORE ]]; then rm -f "$RESTORE"; echo "Deleted $RESTORE"; fi

# ---------------------------------------------------------------- done
step "Status"
systemctl --no-pager --lines=0 status mpd mosquitto zigbee2mqtt tplayer tplayer-web || true
if [[ -f $CF_TOKEN ]]; then systemctl --no-pager --lines=0 status cloudflared-tplayer || true; fi
cat <<EOF

Done. Plug in the Zigbee dongle and USB speakers if they are not in yet. Next:
  1. Copy music, one folder per playlist:
       rsync -av ~/kids-music/ $OWNER@$(hostname):$MUSIC/
     e.g. $MUSIC/1-nursery/..., $MUSIC/2-greek/...   (playlists rotate in name order)
  2. Pair remote:  tplayer-pair
  3. Test:         top = play/pause (double press = next track), bottom = next playlist.
  4. Add YouTube music from a phone or browser:  http://$(hostname).local:$WEB_PORT/
Logs: journalctl -u zigbee2mqtt -u tplayer -u tplayer-web -u mpd -f
EOF

# The ALSA card-0 reservation and the kernel command line apply only after a reboot. Do not reboot from
# here: under cloud-init that can make the first-boot run repeat. flash-mac.sh's
# user-data reboots once cloud-init has finished (power_state).
echo "Reboot once to apply the audio and kernel settings (a flash-mac.sh card does this by itself)."
