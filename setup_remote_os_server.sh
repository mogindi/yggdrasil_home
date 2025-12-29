#!/bin/bash

set -e

server=$1

if [[ -z $server ]]; then
  echo Missing Server
  exit 1
elif ! grep -q $server ~/.ssh/config; then
  echo Server not found in ssh config. Exiting..
  exit 1
fi

# initial user
user=$2
if [[ -z $user ]]; then
  user=root
fi

additional_keys_segment="
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDvHbFftgNosvB4n5OW/OgTllzMK3jPNmC0G6Z1L9kDhT25eLqLk1/bKPUEkRxQdr+hVXNWMLJACzFEeDWhkA1dhz1+GP6FErL81+cW4thC0PfwHnT6ZxmUvBrWvSMqCv8l3sNSSKb9JWXzWu8jRApA1wB9Y4OWIQceADGnAVTq1AhOTweHL2/oZP/2gqyIb57g2YccDVjARgFCUX01AidmNRyG1N3/rMdBKcyJnMQVVjfp+LNlHnYRMVUGwKtBkrjvI6nMoxuqwb599tk7+m9rF4phSEJ12+mVogservCbnTQmDVnEwYnhlA4oUBCjIrL1YmbeKVihgcpZFjdhTNDa/jqzcxI2JW4EQdjPtUxKLd/kzoLASpLKzD+6QqfwpJnep2tcOvtziZO8gCWCkhh70+oUx6uIVe2vl2br+YarwU1B7A3dNz9fv4U0D8tFxwQZbFvQLHdgfjnAAanuk1qlVYyAIBupyaYuHKbJS/h1OGrYDlihDhHFJHRy0x9cqqc= mogindi@MO-SHINOBEE-1
"

> ~/.ssh/known_hosts
ssh-copy-id -o "StrictHostKeyChecking=no" $user@$server
echo "adding extra keys"
ssh $user@$server "echo \"$additional_keys_segment\" | xargs -I% grep -q % ~/.ssh/authorized_keys || echo \"$additional_keys_segment\" | tee -a ~/.ssh/authorized_keys"
echo "generating key"
ssh $user@$server 'ls ~/.ssh/id_rsa.pub || ssh-keygen -b 2048 -t rsa -f ~/.ssh/id_rsa -q -N ""'
echo "adding local key"
ssh $user@$server "cat ~/.ssh/id_rsa.pub | xargs -I% grep -q % ~/.ssh/authorized_keys || cat ~/.ssh/id_rsa.pub | tee -a ~/.ssh/authorized_keys"
echo "moving .ssh folder to root"
ssh $user@$server 'sudo rm -rf /root/.ssh/ && sudo cp -r ~/.ssh/ /root/ && sudo chown root:root -R /root/.ssh/'

ssh $server bash -s <<EOF
set -x

find /etc/ssh/sshd_config* -type f | xargs sed -i 's/.*PasswordAuthentication yes/PasswordAuthentication no/g'
rm -f /etc/ssh/sshd_config.d/*
systemctl restart sshd
hostname $server
echo $server | tee /etc/hostname
EOF
