#!/bin/bash

# find all security groups that dont belong to any of the current projects and delete

openstack project list -f value -c ID | xargs | sed 's/ /|/g' | xargs -I% bash -c 'openstack security group list -f value -c ID -c Project | egrep -v "%"' | awk '{print $1}' |  xargs -r openstack security group delete 
