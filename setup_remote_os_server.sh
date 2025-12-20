#!/bin/bash

server=$1

if [[ -z $server ]]; then
  echo Missing Server
  exit 1
elif ! grep -q $server ~/.ssh/config; then
  echo Server not found in ssh config. Exiting..
  exit 1
fi


# user is only initial user to connection
if [[ $server =~ ovh ]]; then
  user=ubuntu
elif [[ $server =~ hetzner ]]; then
  user=root
fi



additional_keys_segment="
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC2ChoqYodykOqMHA6JX4b2B9HZGdbTHpshh3Djt7dYAv1PuVNOaJSm7k65pGppRduvx5BeUhOJV73g9Jar4hHb9sbDYyG2eR0knalOqwfyOOEE2WmVy2FXjxXiC3yPDpPh5JyysQ9Y7PRjYdvjHVlV/aFSqbYrYULZQRw0UkLbObu6y/eUQfg26UmeSYax7CUn4YDWe2Nko+XzlR1gH7Vmj1DzlTMdNdRGAoC0w86OQR4gGQfnDLpoWM8k5rOuNvZHW19hF+d6l0avAfUjoGhConuevjA9KMopLmIEHTnpSXTdOsBeaVpKToIjbToNIgTZTVMOrFv+YbP/2fricElr EXT.mohamed.el.gindi@W-PC2319M5
"

> ~/.ssh/known_hosts
ssh-copy-id -o "StrictHostKeyChecking=no" $user@$server
echo "adding extra keys"
ssh $user@$server "echo \"$additional_keys_segment\" | tee -a ~/.ssh/authorized_keys"
echo "adding local key"
ssh $user@$server "cat ~/.ssh/id_rsa.pub | tee -a ~/.ssh/authorized_keys"
echo "copying authorized keys to root"
ssh $user@$server 'sudo cp ~/.ssh/authorized_keys /root/.ssh/authorized_keys'


ssh $server bash -s <<EOF
set -x

find /etc/ssh/sshd_config* -type f | xargs sed -i 's/.*PasswordAuthentication yes/PasswordAuthentication no/g'
rm -f /etc/ssh/sshd_config.d/*
systemctl restart sshd
hostname $server
echo $server | tee /etc/hostname
EOF
