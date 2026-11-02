#!/usr/bin/env bash
# Run once on EC2. Does not expose a port or change the installed HTTPS config.
set -euo pipefail
sudo apt-get update
sudo apt-get install -y --no-install-recommends xvfb x11vnc novnc
test -f /usr/share/novnc/core/rfb.js
printf '%s\n' 'Browser login dependencies installed.' \
  'Add the /api/remote-login/ location from deploy/nginx-leads.conf to the existing HTTPS server block.' \
  'Keep its existing certificate directives. Run sudo nginx -t before reloading nginx.' \
  'No EC2 security-group ports, VPN, SSH tunnel, or standalone VNC service are needed.'
