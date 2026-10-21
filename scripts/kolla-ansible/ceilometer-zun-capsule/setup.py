from setuptools import find_packages, setup


setup(
    name="yggdrasil-ceilometer-zun-capsule",
    version="0.1.0",
    packages=find_packages(),
    entry_points={
        "ceilometer.poll.central": [
            "yggdrasil.zun.capsule.cpu_util = "
            "yggdrasil_ceilometer_zun_capsule.cpu:CapsuleCpuUtilPollster"
        ]
    },
)
