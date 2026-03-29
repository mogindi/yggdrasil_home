#!/bin/bash

shopt -s nocasematch

output_file=/tmp/custom_metrics/docker_containers_status.json


docker_ps=$(docker ps -a --format '{{ .Names }},{{ .Status }}')
# open curly brackets
metrics={

while read -r cont
do
    container_name=$(echo $cont | cut -d ',' -f 1 | sed 's/-/_/g')
    container_status=$(echo $cont | cut -d ',' -f 2)
    container_status_code=-1
    if [[ $container_status =~ up.*healthy.* ]]; then
        container_status_code=1
    elif [[ $container_status =~ up.*unhealthy.* ]]; then
        container_status_code=0
    elif [[ $container_status =~ up.*starting.* ]]; then
        container_status_code=0.5
    # no health check
    elif [[ $container_status =~ up.* ]]; then
        container_status_code=1
    elif [[ $container_status =~ exit.* ]]; then
        container_status_code=0
    fi
    metrics=$(echo -e  "$metrics\n\"docker_container_${container_name}_healthy\" : $container_status_code, ")
done <<< "$docker_ps"
# close curl brackets
metrics=$(echo "${metrics::-2}}")

echo "$metrics" > $output_file
