#!/bin/bash


read -p "WARNING. Destructive cleaning! Continue? (y/N) " -n 1 -r
if [[ ! $REPLY =~ ^[Yy]$ ]]
then
  exit 1
fi

set -x

# clear journalctl logs
sudo journalctl --vacuum-size=500M

# clear logs in /var/log
find /var/log -type f -name "*.log.*" | xargs rm -rf

# clear docker container logs
find /var/lib/docker/containers -type f -name "*.log" | xargs -I% bash -c '>%'

# clear docker resources
docker system df -v
docker system prune -a --force

# delete old "flog" indices
curl -X DELETE 'http://10.0.10.100:9200/flog-*?expand_wildcards=all'

# Maybe set index template to make "number_of_replicas": 0 for new flog indices?



