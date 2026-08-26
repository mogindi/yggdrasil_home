#!/bin/bash

set -x

systemctl stop clamav-freshclam.service clamav-daemon

# symlink newer binaries 
ln -sf /usr/local/bin/*clam* /usr/bin/
ln -sf /usr/local/sbin/*clam* /usr/sbin/
ln -sf /etc/clamav/* /usr/local/etc/

ensure_config_line_added () {
  line=$1
  grep "$line" /etc/clamav/clamd.conf || (echo $line | tee -a /etc/clamav/clamd.conf)
}

ensure_config_line_added 'ExcludePath ^/dev/'
ensure_config_line_added 'ExcludePath ^/proc/'
ensure_config_line_added 'ExcludePath ^/sys/'
ensure_config_line_added 'ExcludePath ^/var/lib/docker/'
ensure_config_line_added 'ExcludePath ^/var/lib/libvirt/'
ensure_config_line_added 'ExcludePath ^/run/docker/'
ensure_config_line_added 'ExcludePath ^/run/containerd/'
ensure_config_line_added 'ExcludePath ^/mnt/'
ensure_config_line_added 'ExcludePath ^/snap/'

systemctl start clamav-freshclam.service clamav-daemon
systemctl enable clamav-freshclam.service clamav-daemon

#cat > /etc/cron.daily/clamav_scan.sh <<EOF
##!/bin/bash
#clamdscan --fdpass /
#EOF
