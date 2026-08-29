ARG BASE_IMAGE=quay.io/openstack.kolla/openstack-base:2025.2-ubuntu-noble
FROM ${BASE_IMAGE}

ARG SENLIN_VERSION=16.0.0

ENV PATH=/usr/local/bin:/var/lib/kolla/venv/bin:$PATH
ENV PIP_INDEX_URL=
ENV PIP_EXTRA_INDEX_URL=

# Senlin is no longer shipped as a Kolla image. Keep the service account and
# Kolla entrypoint conventions used by the restored Kolla-Ansible role.
RUN if ! getent group senlin >/dev/null; then groupadd --gid 42443 senlin; fi \
    && if ! getent passwd senlin >/dev/null; then \
        useradd -l -M --shell /usr/sbin/nologin --uid 42443 --gid senlin senlin; \
    fi \
    && usermod --append --groups kolla senlin \
    && mkdir -p /home/senlin /var/lib/senlin /etc/senlin /var/log/kolla/senlin \
    && chown -R senlin:senlin /home/senlin /var/lib/senlin /etc/senlin /var/log/kolla/senlin

RUN /var/lib/kolla/venv/bin/pip install --no-cache-dir \
    -c /requirements/upper-constraints.txt \
    senlin==${SENLIN_VERSION}

COPY api-paste.ini /etc/senlin/api-paste.ini
COPY kolla_extend_start /usr/local/bin/kolla_extend_start
COPY kolla_senlin_extend_start /usr/local/bin/kolla_senlin_extend_start

USER senlin
