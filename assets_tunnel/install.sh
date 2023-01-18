#!/usr/bin/env sh
set -ex

ls /home/tunnel
mkdir -p /home/tunnel/mon/
cp config.json /home/tunnel/mon/mon-config.json
chmod 0640 /home/tunnel/mon/mon-config.json
chown tunnel:tunnel -R /home/tunnel/mon
chown tunnel:tunnel /home/tunnel/mon/mon-config.json

cp ph4conmon.sh /home/tunnel/mon
chmod 0755 /home/tunnel/mon/ph4conmon.sh

cp ph4conmon.service /etc/systemd/system/ph4conmon.service
systemctl daemon-reload

set +ex
echo "[+] DONE"

systemctl enable ph4conmon.service

# systemctl restart ph4upsmon.service
# systemctl restart nut-monitor.service
# pip install -U . && systemctl restart ph4upsmon.service
