#!/bin/bash

set -e

systemctl enable --now auditd.service
