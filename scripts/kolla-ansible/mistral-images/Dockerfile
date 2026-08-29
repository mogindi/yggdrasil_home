ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

# Kolla's Mistral image intentionally ships only the workflow engine.  The
# OpenStack client-backed actions live in mistral-extra, and the ETL workflow
# needs the Barbican and Zun providers so that it can authenticate with the
# caller's project context.
RUN /var/lib/kolla/venv/bin/pip install \
      --no-cache-dir \
      --disable-pip-version-check \
      --no-deps \
      mistral-extra==14.0.0 \
      python-zunclient==5.5.0 \
      python-barbicanclient==5.2.0

USER mistral
