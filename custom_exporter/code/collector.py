from prometheus_client import start_http_server, Gauge
import json, os, time

script_dir = "/tmp/custom_metrics"

metric_list = []
metric_object_list = []

# Decorate function with metric.
def parse_metrics(path):
    f = open(path, "r")
    contents_json = json.loads(f.read())
    for metric, value in contents_json.items():
        index = None
        if metric in metric_list:
            index = metric_list.index(metric)
        else:
            metric_list.append(metric)
            metric_object_list.append(Gauge(metric, 'Custom metric in file ' + path))
            index = -1
        metric_object_list[index].set(value)

if __name__ == '__main__':
    # Start up the server to expose the metrics.
    start_http_server(8079)
    # Generate some requests.
    read_files = []
    while True:
        files = os.listdir(script_dir)
        for file in files:
            file_full_path = script_dir + "/" + file
            parse_metrics(file_full_path)
        time.sleep(5)
