#!/usr/bin/env python3
"""Enable containerd CRI and register Kata as a named runtime handler."""

import argparse
import ast
import datetime
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


CRI_PLUGIN_IDS = {
    "cri",
    "io.containerd.grpc.v1.cri",
    "io.containerd.cri.v1.images",
    "io.containerd.cri.v1.runtime",
}


def migrate_configuration(containerd, config_path):
    result = subprocess.run(
        [str(containerd), "--config", str(config_path), "config", "migrate"],
        check=True,
        capture_output=True,
        text=True,
    )
    if not result.stdout.strip():
        raise RuntimeError("containerd config migrate returned an empty configuration")
    return result.stdout


def configure_cri(configuration):
    disabled_match = re.search(
        r"(?m)^disabled_plugins\s*=\s*(\[[^\n]*\])\s*(?:#.*)?$",
        configuration,
    )
    if disabled_match is None:
        raise RuntimeError("migrated containerd config has no disabled_plugins array")

    try:
        disabled_plugins = ast.literal_eval(disabled_match.group(1))
    except (SyntaxError, ValueError) as error:
        raise RuntimeError("cannot parse containerd disabled_plugins array") from error
    if not isinstance(disabled_plugins, list) or any(
        not isinstance(plugin, str) for plugin in disabled_plugins
    ):
        raise RuntimeError("containerd disabled_plugins must be an array of strings")

    enabled_plugins = [
        plugin for plugin in disabled_plugins if plugin not in CRI_PLUGIN_IDS
    ]
    replacement = "disabled_plugins = " + repr(enabled_plugins)
    configuration = (
        configuration[: disabled_match.start()]
        + replacement
        + configuration[disabled_match.end() :]
    )

    version_match = re.search(r"(?m)^version\s*=\s*(\d+)\s*$", configuration)
    if version_match is None:
        raise RuntimeError("migrated containerd config has no version header")
    plugin_id = (
        "io.containerd.cri.v1.runtime"
        if int(version_match.group(1)) >= 3
        else "io.containerd.grpc.v1.cri"
    )
    section_pattern = re.compile(
        r"(?m)^[ \t]*\[plugins\.(?:\"|')?"
        + re.escape(plugin_id)
        + r"(?:\"|')?\.containerd\.runtimes\.kata\]\s*$"
    )
    sections = list(section_pattern.finditer(configuration))
    if len(sections) > 1:
        raise RuntimeError("containerd config has multiple Kata runtime handler sections")
    if sections:
        section_start = sections[0].end()
        following_section = re.search(
            r"(?m)^[ \t]*\[", configuration[section_start:]
        )
        section_end = (
            section_start + following_section.start()
            if following_section
            else len(configuration)
        )
        section_body = configuration[section_start:section_end]
        runtime_match = re.search(
            r"(?m)^[ \t]*runtime_type\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$",
            section_body,
        )
        if runtime_match is None or runtime_match.group(2) != "io.containerd.kata.v2":
            raise RuntimeError(
                "containerd Kata handler exists but does not use io.containerd.kata.v2"
            )
        return configuration

    separator = "" if configuration.endswith("\n\n") else "\n"
    configuration += (
        separator
        + f"[plugins.'{plugin_id}'.containerd.runtimes.kata]\n"
        + "  runtime_type = 'io.containerd.kata.v2'\n"
    )
    return configuration


def normalize_configuration(containerd, config_path, configuration):
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=config_path.parent,
        prefix=".containerd-config-",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(configuration)

    try:
        result = subprocess.run(
            [str(containerd), "--config", str(temporary_path), "config", "migrate"],
            check=True,
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            raise RuntimeError("containerd config migrate returned an empty configuration")
        return result.stdout
    finally:
        temporary_path.unlink(missing_ok=True)


def validate_configuration(containerd, config_path, configuration):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=config_path.parent, prefix=".containerd-config-", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(configuration)

    try:
        os.chmod(temporary_path, 0o600)
        subprocess.run(
            [str(containerd), "--config", str(temporary_path), "config", "dump"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        temporary_path.unlink(missing_ok=True)


def install_configuration(config_path, configuration):
    current = config_path.read_text(encoding="utf-8")
    if current == configuration:
        print("unchanged")
        return

    stat_result = config_path.stat()
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = config_path.with_name(f"{config_path.name}.pre-zun-cri.{timestamp}")
    shutil.copy2(config_path, backup_path)
    os.chmod(backup_path, 0o600)

    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=config_path.parent, prefix=f".{config_path.name}.", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(configuration)
        temporary.flush()
        os.fsync(temporary.fileno())

    try:
        os.chmod(temporary_path, stat_result.st_mode & 0o777)
        os.chown(temporary_path, stat_result.st_uid, stat_result.st_gid)
        os.replace(temporary_path, config_path)
        directory_fd = os.open(config_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)
    print("changed")


def containerd_needs_restart(config_path):
    """Return whether the running daemon predates its current config."""
    result = subprocess.run(
        ["systemctl", "show", "--property=MainPID", "--value", "containerd.service"],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        pid = int(result.stdout.strip())
        if pid <= 0:
            return True
        stat_fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        start_ticks = int(stat_fields[19])
        boot_time = next(
            int(line.split()[1])
            for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        process_started_ns = int(
            (boot_time + start_ticks / os.sysconf("SC_CLK_TCK")) * 1_000_000_000
        )
    except (OSError, StopIteration, ValueError, IndexError):
        return True
    return config_path.stat().st_mtime_ns > process_started_ns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config", nargs="?", default="/etc/containerd/config.toml")
    parser.add_argument("--containerd", default="/usr/bin/containerd")
    parser.add_argument(
        "--check-runtime-load",
        action="store_true",
        help="request a daemon restart if its process predates the config file",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    containerd = Path(args.containerd).resolve()
    if not config_path.is_file():
        raise RuntimeError(f"containerd config does not exist: {config_path}")
    if not containerd.is_file():
        raise RuntimeError(f"containerd binary does not exist: {containerd}")

    migrated = migrate_configuration(containerd, config_path)
    configured = normalize_configuration(
        containerd, config_path, configure_cri(migrated)
    )
    validate_configuration(containerd, config_path, configured)
    current = config_path.read_text(encoding="utf-8")
    if current == configured and args.check_runtime_load and containerd_needs_restart(
        config_path
    ):
        print("changed")
    else:
        install_configuration(config_path, configured)


if __name__ == "__main__":
    main()
