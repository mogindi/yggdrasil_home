#!/usr/bin/env python3
"""Use CRI v1 with a fallback for older v1alpha2-only containerd nodes."""

from pathlib import Path
import glob
import sys


DRIVER_CLASS = "class CriDriver(driver.BaseDriver, driver.CapsuleDriver):"
OLD_STUBS = """        self.runtime_stub = api_pb2_grpc.RuntimeServiceStub(channel)
        self.image_stub = api_pb2_grpc.ImageServiceStub(channel)
"""
NEW_STUBS = """        compat_channel = _CriApiCompatibilityChannel(channel)
        self.runtime_stub = api_pb2_grpc.RuntimeServiceStub(compat_channel)
        self.image_stub = api_pb2_grpc.ImageServiceStub(compat_channel)
"""

COMPATIBILITY_CHANNEL = '''class _CriApiCompatibilityChannel:
    """Route CRI calls to v1, falling back only when the runtime lacks v1."""

    _ALPHA2_PREFIX = "/runtime.v1alpha2."
    _V1_PREFIX = "/runtime.v1."

    def __init__(self, channel):
        self._channel = channel
        self._service_versions = {}

    def _paths(self, method):
        if not method.startswith(self._ALPHA2_PREFIX):
            return method, method, None
        service = method.split("/", 2)[1]
        stable_method = method.replace(
            self._ALPHA2_PREFIX, self._V1_PREFIX, 1
        )
        return stable_method, method, service

    def unary_unary(self, method, request_serializer=None,
                    response_deserializer=None, **kwargs):
        stable_method, alpha2_method, service = self._paths(method)
        stable_call = self._channel.unary_unary(
            stable_method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
            **kwargs
        )
        if service is None:
            return stable_call
        alpha2_call = self._channel.unary_unary(
            alpha2_method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
            **kwargs
        )

        def invoke(request, *args, **call_kwargs):
            if self._service_versions.get(service) == "v1alpha2":
                return alpha2_call(request, *args, **call_kwargs)
            try:
                result = stable_call(request, *args, **call_kwargs)
            except grpc.RpcError as error:
                if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                    raise
                self._service_versions[service] = "v1alpha2"
                LOG.info(
                    "CRI service %s does not implement v1; falling back to v1alpha2",
                    service,
                )
                return alpha2_call(request, *args, **call_kwargs)
            self._service_versions[service] = "v1"
            return result

        return invoke

    def unary_stream(self, method, request_serializer=None,
                     response_deserializer=None, **kwargs):
        stable_method, alpha2_method, service = self._paths(method)
        stable_call = self._channel.unary_stream(
            stable_method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
            **kwargs
        )
        if service is None:
            return stable_call
        alpha2_call = self._channel.unary_stream(
            alpha2_method,
            request_serializer=request_serializer,
            response_deserializer=response_deserializer,
            **kwargs
        )

        def invoke(request, *args, **call_kwargs):
            if self._service_versions.get(service) == "v1alpha2":
                return alpha2_call(request, *args, **call_kwargs)
            try:
                responses = stable_call(request, *args, **call_kwargs)
                first = next(responses)
            except StopIteration:
                self._service_versions[service] = "v1"
                return iter(())
            except grpc.RpcError as error:
                if error.code() != grpc.StatusCode.UNIMPLEMENTED:
                    raise
                self._service_versions[service] = "v1alpha2"
                LOG.info(
                    "CRI service %s does not implement v1; falling back to v1alpha2",
                    service,
                )
                return alpha2_call(request, *args, **call_kwargs)
            self._service_versions[service] = "v1"

            def stream():
                yield first
                yield from responses

            return stream()

        return invoke

    def __getattr__(self, name):
        return getattr(self._channel, name)
'''


def candidate_paths() -> list[Path]:
    if len(sys.argv) > 1:
        return [Path(argument) for argument in sys.argv[1:]]
    return sorted({
        Path(path).resolve()
        for path in glob.glob(
            "/var/lib/kolla/venv/lib/python*/site-packages/"
            "zun/container/cri/driver.py"
        )
    })


def patch(path: Path) -> None:
    source = path.read_text()
    if COMPATIBILITY_CHANNEL in source and NEW_STUBS in source:
        return
    if COMPATIBILITY_CHANNEL in source or NEW_STUBS in source:
        raise RuntimeError(f"CRI compatibility patch is incomplete in {path}")
    if source.count(DRIVER_CLASS) != 1 or source.count(OLD_STUBS) != 1:
        raise RuntimeError(
            f"expected one supported Zun CRI driver layout in {path}; "
            f"found driver class {source.count(DRIVER_CLASS)} times and "
            f"stub setup {source.count(OLD_STUBS)} times"
        )

    source = source.replace(
        DRIVER_CLASS, COMPATIBILITY_CHANNEL + "\n\n" + DRIVER_CLASS, 1
    )
    source = source.replace(OLD_STUBS, NEW_STUBS, 1)
    path.write_text(source)


def main() -> None:
    paths = candidate_paths()
    if len(paths) != 1:
        raise RuntimeError(
            f"expected one Zun CRI driver module, found {len(paths)}: {paths}"
        )
    patch(paths[0])


if __name__ == "__main__":
    main()
