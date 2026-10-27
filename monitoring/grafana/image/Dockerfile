FROM grafana/grafana:11.2.0

# Bake provisioning so USB AppleDouble (._*) files never crash Grafana.
USER root
COPY provisioning /etc/grafana/provisioning
RUN chown -R grafana:root /etc/grafana/provisioning \
 && find /etc/grafana/provisioning -type d -exec chmod 755 {} \; \
 && find /etc/grafana/provisioning -type f -exec chmod 644 {} \;
USER grafana
