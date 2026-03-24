mkdir -p /tmp/custom_metrics

docker rm -f custom_exporter

docker run -it --name custom_exporter -d \
--network host \
--restart always \
-v /tmp/custom_metrics:/tmp/custom_metrics \
custom_exporter
