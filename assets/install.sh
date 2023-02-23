#!/usr/bin/env bash
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
set -ex

cd "${SCRIPT_DIR}"
cp config.json /etc/nut/upsmon-config.json
chmod 0640 /etc/nut/upsmon-config.json
chown nut:nut /etc/nut/upsmon-config.json

cp ph4upsmon.sh ph4upsmon-notif.sh /etc/nut/
chmod 0755 /etc/nut/ph4upsmon.sh
chmod 0755 /etc/nut/ph4upsmon-notif.sh

touch /var/log/ph4upsmon.json
chown nut:nut /var/log/ph4upsmon.json

cp ph4upsmon.service /etc/systemd/system/ph4upsmon.service
systemctl daemon-reload

set +ex
echo "[+] DONE"
echo "Make sure that /etc/nut/upsmon.conf has the NOTIFYCMD and NOTIFYFLAG directives set correctly:"
echo "
NOTIFYCMD \"/etc/nut/ph4upsmon-notif.sh\"
NOTIFYFLAG ONLINE	SYSLOG+WALL+EXEC
NOTIFYFLAG ONBATT	SYSLOG+WALL+EXEC
NOTIFYFLAG LOWBATT	SYSLOG+WALL+EXEC
NOTIFYFLAG FSD	SYSLOG+WALL+EXEC
NOTIFYFLAG COMMOK	SYSLOG+WALL+EXEC
NOTIFYFLAG COMMBAD	SYSLOG+WALL+EXEC
NOTIFYFLAG SHUTDOWN	SYSLOG+WALL+EXEC
NOTIFYFLAG REPLBATT	SYSLOG+WALL+EXEC
NOTIFYFLAG NOCOMM	SYSLOG+WALL+EXEC
NOTIFYFLAG NOPARENT	SYSLOG+WALL+EXEC"

systemctl enable ph4upsmon.service

# systemctl restart ph4upsmon.service
# systemctl restart nut-monitor.service
# pip install -U . && systemctl restart ph4upsmon.service
