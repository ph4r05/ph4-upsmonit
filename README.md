# UPS monitoring

[NUT] UPS monitoring and notifications via Telegram notification or email or both.

## Use case
Connect your primary internet router to UPS (e.g., CyberPower) to protect your network internet connectivity (and internal WiFi, ZigBee) from power outage (e.g., accident or sabotage). You may have backup LTE modem installed should power outage disable the primary link.

With UPS backup your internal network keeps running. Commodity models of UPSs do not provide notification on status change so if you are not physically present on premise, you won't get notified of such event.

This package helps with that. It is supposed to be running on a PC (e.g., RPi) connected via USB to the UPS, powered by the UPS as well.
On the PC, [NUT] should be configured to monitor UPS state ([setup tutorial](https://www.howtoraspberry.com/2020/11/how-to-monitor-ups-with-raspberry-pi/)).

This package then collects events from [NUT] and continuously monitors UPSs state. For example, when UPS state changes, you get an email and Telegram notification about such event. Also, if UPS is running on battery, you get periodic heartbeat status message on Telegram so you know expected battery running time and to see that system is still operating.

```
                              ┌──────────┐
                    USB link  │    RPi   │
                  ┌───────────┤          │
                  │           └───┬────┬─┘
           ┌──────┴───────┐       │    │
┌──────┐   │              │       │
│Power ├───┤     UPS      ├───────┤    │
└──────┘   │              │       │     Eth
           └──────────────┘       │    │
                              ┌───┴────┴─┐
                              │   WiFi   │
                              │  router  │
                              └──────────┘
```

## Setup

- pip-install this package `pip install -U ph4-upsmonit`
- Configure `config.json` according to [assets/config-example.json](assets/config-example.json)
- Configure notification channels, either 
  - [Telegram bot](https://www.teleme.io/articles/create_your_own_telegram_bot?hl=en) 
  - or Email sender (e.g., [Gmail](https://www.lifewire.com/get-a-password-to-access-gmail-by-pop-imap-2-1171882)) or both
- Run `assets/install.sh` to install `ph4upsmon.service` systemd service
- Configure [NUT] to send events to `ph4upsmon` as install script instructs you
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

Telegram channel gets updated regularly until state of the UPS returns back to normal. Note that `meta.battery.runtime.m` field shows remaining battery time estimation in minutes.

Telegram bot supports also several commands, e.g., `/status` and `/full_status`, to which it responds with current state. You can manually request status information and to check that system is responsive.

Note that Email notifier sends only state changes, while Telegram notifier sends also regular state updates when UPS is running on the battery.
If email user is empty, email notifier is not use. Likewise, if bot API key is empty, telegram is not used.

## Usage

Send `/help` message to the Telegram bot

```
Help: 
/start - register
/stop - deregister
/status - brief status
/full_status - full status
/log - log of latest events
/noemail - disable email reporting
/doemail - enable email reporting
/doedit <time> - edit last status message instead of sending a new one. Time to edit the old message in seconds.
```

## Dependencies
This project uses monitoring tool library https://github.com/ph4r05/ph4-monitlib

Similar project for monitoring network connections (SSH tunnels): https://github.com/ph4r05/ph4-connmon

[NUT]: https://networkupstools.org
