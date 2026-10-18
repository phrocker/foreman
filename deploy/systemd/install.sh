#!/bin/sh
# Install the user units and make them outlive the login session.
set -e
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.config/systemd/user"
for f in foreman-store.service foreman-dashboard.service foreman-sweep.service foreman-sweep.timer; do
  ln -sf "$here/$f" "$HOME/.config/systemd/user/$f"
done
# The units run with systemd's own PATH, which does not include wherever the
# operator installed gcloud or kubectl. Under the timer, every cloud and
# runtime collection then reported "the gcloud CLI is not installed" while the
# same command worked from a shell. A drop-in gives both units the directories
# those tools were found in at install time, ahead of the default path.
tooldirs=""
for tool in gcloud kubectl gh; do
  found=$(command -v "$tool" 2>/dev/null) || continue
  dir=$(dirname "$found")
  case ":$tooldirs:" in *":$dir:"*) ;; *) tooldirs="${tooldirs:+$tooldirs:}$dir" ;; esac
done
for unit in foreman-dashboard.service foreman-sweep.service; do
  mkdir -p "$HOME/.config/systemd/user/$unit.d"
  cat > "$HOME/.config/systemd/user/$unit.d/tools.conf" <<CONF
# Written by deploy/systemd/install.sh: where gcloud, kubectl and gh were found.
[Service]
Environment="PATH=${tooldirs:+$tooldirs:}$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CONF
done
systemctl --user daemon-reload
# Without linger, user services stop at logout and never start at boot.
loginctl enable-linger "$USER" 2>/dev/null || echo "enable-linger needs: sudo loginctl enable-linger $USER"
systemctl --user enable foreman-store.service foreman-dashboard.service foreman-sweep.timer
echo "installed; start with: systemctl --user start foreman-store foreman-dashboard foreman-sweep.timer"
