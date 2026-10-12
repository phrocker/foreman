#!/bin/sh
# Install the user units and make them outlive the login session.
set -e
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$HOME/.config/systemd/user"
for f in foreman-store.service foreman-dashboard.service foreman-sweep.service foreman-sweep.timer; do
  ln -sf "$here/$f" "$HOME/.config/systemd/user/$f"
done
systemctl --user daemon-reload
# Without linger, user services stop at logout and never start at boot.
loginctl enable-linger "$USER" 2>/dev/null || echo "enable-linger needs: sudo loginctl enable-linger $USER"
systemctl --user enable foreman-store.service foreman-dashboard.service foreman-sweep.timer
echo "installed; start with: systemctl --user start foreman-store foreman-dashboard foreman-sweep.timer"
