#!/usr/bin/env python3
"""Teach Zun's interactive exec proxy to authenticate to Docker over TLS."""

from pathlib import Path
import glob
import sys


OLD = "        client = docker.APIClient(base_url=exec_instance.url)"
NEW = """        tls_config = False
        if not CONF.docker.api_insecure:
            tls_config = docker.tls.TLSConfig(
                client_cert=(CONF.docker.cert_file, CONF.docker.key_file),
                verify=CONF.docker.ca_file)

        client = docker.APIClient(
            base_url=exec_instance.url,
            version=CONF.docker.docker_remote_api_version,
            tls=tls_config)"""


def candidate_paths() -> list[Path]:
    if len(sys.argv) > 1:
        return [Path(argument) for argument in sys.argv[1:]]
    return sorted({
        Path(path).resolve()
        for path in glob.glob(
            "/var/lib/kolla/venv/lib/python*/site-packages/"
            "zun/websocket/websocketproxy.py"
        )
    })


def patch(path: Path) -> None:
    source = path.read_text()
    if NEW in source and OLD not in source:
        return
    if source.count(OLD) != 1:
        raise RuntimeError(
            f"expected exactly one unpatched Docker API client in {path}, "
            f"found {source.count(OLD)}"
        )
    path.write_text(source.replace(OLD, NEW))


def main() -> None:
    paths = candidate_paths()
    if len(paths) != 1:
        raise RuntimeError(
            f"expected one Zun websocket proxy module, found {len(paths)}: {paths}"
        )
    patch(paths[0])


if __name__ == "__main__":
    main()
