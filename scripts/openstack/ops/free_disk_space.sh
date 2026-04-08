
#!/bin/bash


# clear journalctl logs
sudo journalctl --vacuum-size=500M

# clear docker resources
docker system df -v
echo RUN `docker system prune -a`

# delete old "flog" indices
curl -X DELETE 'http://10.0.10.100:9200/flog-*?expand_wildcards=all'

# Maybe set index template to make "number_of_replicas": 0 for new flog indices?
