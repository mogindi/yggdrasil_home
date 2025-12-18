# Prerequisites #

### On local Computer: ###

1. Setup server from OVH or Hetzner GUI with password authentication

2. On local computer, ensure os01 (for OVH) and os02 (for Hetzner) is added: 
```
grep -q os01 ~/.ssh/config || cat >> ~/.ssh/config <<EOF

# ovh
Host os01-ovh
    Hostname 188.165.251.200
    User root
    ForwardAgent yes
EOF
grep -q os02 ~/.ssh/config || cat >> ~/.ssh/config <<EOF

# hetzner
Host os01-hetzner
    Hostname 144.76.28.49
    User root
    ForwardAgent yes
EOF
```

3. Clone this repo
```
git clone git@bitbucket.org:mgindi/kolla-deploy.git && cd kolla-deploy
```

4. Run script to setup remote server
```
./setup_remote_os_server.sh os01-hetzner
```

5. Connect to remote server
```
ssh os01-hetzner
```

### On Remote Server: ###

6. Setup prerequisites
```
bash -s <<EOF
set -xe
apt update
apt install -y git make ansible bash-completion
ansible-galaxy collection install ansible.netcommon:2.5.1
echo "set -g history-limit 10000" > ~/.tmux.conf
echo "set paste" > ~/.vimrc

cat << 'EOT' > ~/.ssh/rc
#!/bin/bash
latest_ssh_auth_sock=\$(ls -dt /tmp/ssh-*/agent* | head -n 1)
ln -sf \$latest_ssh_auth_sock ~/.ssh/ssh_auth_sock
EOT
sed -i 's/.*PermitUserEnvironment.*/PermitUserEnvironment yes/g' /etc/ssh/sshd_config
systemctl restart ssh
echo 'SSH_AUTH_SOCK=/root/.ssh/ssh_auth_sock' > ~/.ssh/environment

cd ~
ls kolla-deploy || GIT_SSH_COMMAND="ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no" git clone git@bitbucket.org:mgindi/kolla-deploy.git
cd kolla-deploy
git config --global user.email "mo.gindi@gmail.com"
git config --global user.name "Mohamed El Gindi"
git pull

EOF

bash ~/.ssh/rc
export SSH_AUTH_SOCK=/root/.ssh/ssh_auth_sock
cd kolla-deploy
tmux
```
