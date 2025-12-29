#!/bin/bash

if [[ -z $NETWORK_PROVIDER_NET ]]; then
  echo Empty NETWORK_PROVIDER_NET Env Var. Exitting..
  exit=1
fi
if [[ -z $OPENSTACK_MGMT_NET ]]; then
  echo Empty OPENSTACK_MGMT_NET Env Var. Exitting..
  exit=1
fi
if [[ -z $CEPH_PUBLIC_NET ]]; then
  echo Empty CEPH_PUBLIC_NET Env Var. Exitting..
  exit=1
fi
if [[ -z $CEPH_CLUSTER_NET ]]; then
  echo Empty CEPH_CLUSTER_NET Env Var. Exitting..
  exit=1
fi
if [[ -z $NETWORK_PROVIDER_NET ]]; then
  echo Empty NETWORK_PROVIDER_NET Env Var. Exitting..
  exit=1
fi
if [[ -z $DEPLOYMENT_NODE_IPS ]]; then
  echo Empty DEPLOYMENT_NODE_IPS Env Var. Exitting..
  exit=1
fi

if [[ $exit -eq 1 ]]; then
  exit 1
fi

rules=(
  "\-A INPUT -i lo -j ACCEPT -m comment --comment 'allow loopback, needed for dns purposes'"
  "\-A INPUT -p icmp -j ACCEPT -m comment --comment 'allow ping'"
  "\-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT -m comment --comment 'allow established connections'" 
  "\-A INPUT -s $NETWORK_PROVIDER_NET -p tcp -m tcp --dport 22 -j DROP -m comment --comment 'block ssh from provider network'"
  "\-A INPUT -p tcp -m tcp --dport 22 -j ACCEPT -m comment --comment 'allow ssh from other networks'"
  "\-A INPUT -s $OPENSTACK_MGMT_NET -j ACCEPT -m comment --comment 'allow openstack management network'"
  "\-A INPUT -s $CEPH_PUBLIC_NET -j ACCEPT -m comment --comment 'allow ceph public network'"
  "\-A INPUT -s $CEPH_CLUSTER_NET -j ACCEPT -m comment --comment 'allow ceph cluster network'"
  "\-A INPUT -s $NETWORK_PROVIDER_NET -j ACCEPT -m comment --comment 'allow provider network'"
  "\-A INPUT -s $VPN_NET -j ACCEPT -m comment --comment 'allow vpn network'"
  "\-A INPUT -p tcp --dport 443 -m state --state NEW --syn -m hashlimit --hashlimit 15/s --hashlimit-burst 30 --hashlimit-mode srcip --hashlimit-srcmask 32 --hashlimit-name synattack -j ACCEPT -m comment --comment 'limit new syn connections to port 443 (yggdrasil console)'"
  "\-A INPUT -p tcp --dport 8443 -j ACCEPT -m comment --comment 'allow nova console access'"
  "\-A INPUT -i lxdbr0 -j ACCEPT -m comment --comment 'allow lxd bridge traffic for dev vms'"
)


for node_ip in $(echo ${DEPLOYMENT_NODE_IPS} | xargs); do
rules=(
  "${rules[@]}"
  "\-A INPUT -s $node_ip -j ACCEPT -m comment --comment 'allow traffic from deployment node $node_ip'"
)
done

set -x

iptables --policy INPUT ACCEPT

iptables --flush INPUT


for rule in "${rules[@]}"; do
  (iptables-save | grep -q "$rule") || (echo "Adding rule \"$rule\"" && eval "iptables $rule")
done

iptables --policy INPUT DROP
