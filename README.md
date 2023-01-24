# UPS monitoring

[NUT] UPS monitoring and notifications via Telegram notification and email

## Use case
Connect your primary internet router to UPS (e.g., CyberPower) to protect your network internet connectivity (and internal WiFi, ZigBee) from power outage (e.g., accident or sabotage). You may have backup LTE modem installed should power outage disable the primary link.

With UPS backup your internal network keeps running. Commodity models of UPSs do not provide notification on status change so if you are not physically present on premise, you won't get notified of such event.

This package helps with that. It is supposed to be running on a PC (e.g., RPi) connected via USB to the UPS, powered by the UPS as well.
On the PC, [NUT] should be configured to monitor UPS state ([setup tutorial](https://www.howtoraspberry.com/2020/11/how-to-monitor-ups-with-raspberry-pi/)).

This package then collects events from [NUT] and continuously monitors UPSs state. For example, when UPS state changes, you get an email and Telegram notification about such event. Also, if UPS is running on battery, you get periodic heartbeat status message on Telegram so you know expected battery running time and to see that system is still operating

## Setup

- pip-install this package
- Configure `config.json` according to `assets/config-example.json`
- Configure either [Telegram bot](https://www.teleme.io/articles/create_your_own_telegram_bot?hl=en) or Email sender or both
- Run `assets/install.sh` to install `ph4upsmon.service` systemd service
- Configure [NUT] to send events to ph4upsmon as install script instructs you
- Run `systemctl start ph4upsmon.service`

## Notification examples

System startup:

```
UPS state report [OL, age=0.00]: {
  "battery.charge": 100.0,
  "battery.runtime": 13370.0,
  "battery.voltage": 26.8,
  "input.voltage": 242.0,
  "output.voltage": 242.0,
  "ups.load": 0.0,
  "ups.status": "OL",
  "ups.test.result": "No test initiated",
  "meta.battery.runtime.m": 222.6,
  "meta.time_check": 1674067083.7017221,
  "meta.dt_check": "01/18/2023, 18:38:04"
}
```

System is running on battery

```
UPS state report [OB DISCHRG, age=0.00]: {
  "battery.charge": 100.0,
  "battery.runtime": 13370.0,
  "battery.voltage": 26.8,
  "input.voltage": 242.0,
  "output.voltage": 242.0,
  "ups.load": 0.0,
  "ups.status": "OB DISCHRG",
  "ups.test.result": "No test initiated",
  "meta.battery.runtime.m": 222.6,
  "meta.time_check": 1674067076.121453,
  "meta.dt_check": "01/18/2023, 18:37:56"
}
```

[NUT]: https://networkupstools.org