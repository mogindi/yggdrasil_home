ARG BASE_IMAGE=quay.io/openstack.kolla/openstack-base:2025.2-ubuntu-noble
FROM ${BASE_IMAGE}

ARG ZAQAR_REF=stable/2025.2
ARG ZAQAR_PIP_INDEX_URL=https://pypi.org/simple
ARG ZAQAR_PIP_EXTRA_INDEX_URL=

ENV PATH=/usr/local/bin:/var/lib/kolla/venv/bin:$PATH
ENV PIP_INDEX_URL=
ENV PIP_EXTRA_INDEX_URL=

# Zaqar is not shipped as a Kolla image in the 2025.2 service set. Install
# the matching stable branch into the Kolla virtualenv and retain Kolla's
# standard container entrypoint and logging conventions.  42495 is the next
# unused Kolla service-account ID after Valkey in the 2025.2 base image.
RUN if ! getent group zaqar >/dev/null; then groupadd --gid 42495 zaqar; fi \
    && if ! getent passwd zaqar >/dev/null; then \
        useradd -l -M --shell /usr/sbin/nologin --uid 42495 --gid zaqar zaqar; \
    fi \
    && usermod --append --groups kolla zaqar \
    && mkdir -p /home/zaqar /var/lib/zaqar/tmp /etc/zaqar /var/log/kolla/zaqar \
    && chown -R zaqar:zaqar /home/zaqar /var/lib/zaqar /etc/zaqar /var/log/kolla/zaqar

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && PIP_INDEX_URL="${ZAQAR_PIP_INDEX_URL}" \
       PIP_EXTRA_INDEX_URL="${ZAQAR_PIP_EXTRA_INDEX_URL}" \
       /var/lib/kolla/venv/bin/pip install --no-cache-dir \
        -c /requirements/upper-constraints.txt \
        "zaqar[redis] @ git+https://opendev.org/openstack/zaqar.git@${ZAQAR_REF}" \
    && apt-get purge -y git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

COPY extend_start.sh /usr/local/bin/kolla_extend_start

RUN chmod 0755 /usr/local/bin/kolla_extend_start \
    && chown root:root /usr/local/bin/kolla_extend_start
