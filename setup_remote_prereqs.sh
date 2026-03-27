ssh $node bash -s <<EOF
set -xe
apt update
apt install -y git make ansible bash-completion
ansible-galaxy collection install ansible.netcommon ansible.utils --force
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
ls yggdrasil_home || GIT_SSH_COMMAND="ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no" git clone git@github.com:mogindi/yggdrasil_home.git
cd yggdrasil_home
git config --global user.email $git_email
git config --global user.name $git_user
git pull

EOF

bash ~/.ssh/rc
export SSH_AUTH_SOCK=/root/.ssh/ssh_auth_sock
cd yggdrasil_home
tmux
