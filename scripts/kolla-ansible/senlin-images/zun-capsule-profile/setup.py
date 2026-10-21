from setuptools import find_packages, setup


setup(
    name="yggdrasil-senlin-zun-capsule-profile",
    version="0.1.0",
    packages=find_packages(),
    entry_points={
        "senlin.profiles": [
            "os.zun.capsule-1.0 = yggdrasil_senlin_zun_profile.capsule:CapsuleProfile"
        ]
    },
)
