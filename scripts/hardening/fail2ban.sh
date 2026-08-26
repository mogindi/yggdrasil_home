#!/bin/bash

systemctl is-active fail2ban | grep -q ^active || systemctl restart fail2ban
systemctl enable fail2ban
