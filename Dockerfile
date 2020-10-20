FROM adoptopenjdk/openjdk11:jre-11.0.8_10-alpine

# symlink JVM
RUN mkdir -p /usr/lib/jvm/default-jvm /usr/java/latest \
    && ln -sf /opt/java/openjdk /usr/lib/jvm/default-jvm/jre \
    && ln -sf /usr/lib/jvm/default-jvm/jre /usr/java/latest/jre

# ===============
# Alpine packages
# ===============

RUN apk update \
    && apk add --no-cache openssl py3-pip curl tini \
    && apk add --no-cache --virtual build-deps wget git

# ===========
# Auth client
# ===========

# @TODO: this package is deprecated; should downloads all required JARs from jans-auth-server.war
# JAR files required to generate OpenID Connect keys
ENV CLOUD_NATIVE_VERSION=5.0.0-SNAPSHOT
ENV CLOUD_NATIVE_BUILD_DATE="2020-10-17 19:42"
ENV CLOUD_NATIVE_SOURCE_URL=https://maven.jans.io/maven/io/jans/jans-auth-client/${CLOUD_NATIVE_VERSION}/jans-auth-client-${CLOUD_NATIVE_VERSION}-jar-with-dependencies.jar

RUN wget -q ${CLOUD_NATIVE_SOURCE_URL} -P /app/javalibs/
#
# =================
# Shibboleth sealer
# =================

# @TODO: remove them?
RUN wget -q https://build.shibboleth.net/nexus/content/repositories/releases/net/shibboleth/utilities/java-support/7.5.1/java-support-7.5.1.jar -P /app/javalibs/ \
    && wget -q https://repo1.maven.org/maven2/com/beust/jcommander/1.48/jcommander-1.48.jar -P /app/javalibs/ \
    && wget -q https://repo1.maven.org/maven2/org/slf4j/slf4j-api/1.7.26/slf4j-api-1.7.26.jar -P /app/javalibs/ \
    && wget -q https://repo1.maven.org/maven2/org/slf4j/slf4j-simple/1.7.26/slf4j-simple-1.7.26.jar -P /app/javalibs/

# ======
# Python
# ======

RUN apk add --no-cache py3-cryptography
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -U pip \
    && pip3 install --no-cache-dir -r /app/requirements.txt \
    && rm -rf /src/jans-pycloudlib/.git

# =======
# Cleanup
# =======

RUN apk del build-deps \
    && rm -rf /var/cache/apk/*

# =======
# License
# =======

RUN mkdir -p /licenses
COPY LICENSE /licenses/

# ==========
# Config ENV
# ==========

ENV CLOUD_NATIVE_CONFIG_ADAPTER=consul \
    CLOUD_NATIVE_CONFIG_CONSUL_HOST=localhost \
    CLOUD_NATIVE_CONFIG_CONSUL_PORT=8500 \
    CLOUD_NATIVE_CONFIG_CONSUL_CONSISTENCY=default \
    CLOUD_NATIVE_CONFIG_CONSUL_SCHEME=http \
    CLOUD_NATIVE_CONFIG_CONSUL_VERIFY=false \
    CLOUD_NATIVE_CONFIG_CONSUL_CACERT_FILE=/etc/certs/consul_ca.crt \
    CLOUD_NATIVE_CONFIG_CONSUL_CERT_FILE=/etc/certs/consul_client.crt \
    CLOUD_NATIVE_CONFIG_CONSUL_KEY_FILE=/etc/certs/consul_client.key \
    CLOUD_NATIVE_CONFIG_CONSUL_TOKEN_FILE=/etc/certs/consul_token \
    CLOUD_NATIVE_CONFIG_CONSUL_NAMESPACE=jans \
    CLOUD_NATIVE_CONFIG_KUBERNETES_NAMESPACE=default \
    CLOUD_NATIVE_CONFIG_KUBERNETES_CONFIGMAP=jans \
    CLOUD_NATIVE_CONFIG_KUBERNETES_USE_KUBE_CONFIG=false

# ==========
# Secret ENV
# ==========

ENV CLOUD_NATIVE_SECRET_ADAPTER=vault \
    CLOUD_NATIVE_SECRET_VAULT_SCHEME=http \
    CLOUD_NATIVE_SECRET_VAULT_HOST=localhost \
    CLOUD_NATIVE_SECRET_VAULT_PORT=8200 \
    CLOUD_NATIVE_SECRET_VAULT_VERIFY=false \
    CLOUD_NATIVE_SECRET_VAULT_ROLE_ID_FILE=/etc/certs/vault_role_id \
    CLOUD_NATIVE_SECRET_VAULT_SECRET_ID_FILE=/etc/certs/vault_secret_id \
    CLOUD_NATIVE_SECRET_VAULT_CERT_FILE=/etc/certs/vault_client.crt \
    CLOUD_NATIVE_SECRET_VAULT_KEY_FILE=/etc/certs/vault_client.key \
    CLOUD_NATIVE_SECRET_VAULT_CACERT_FILE=/etc/certs/vault_ca.crt \
    CLOUD_NATIVE_SECRET_VAULT_NAMESPACE=jans \
    CLOUD_NATIVE_SECRET_KUBERNETES_NAMESPACE=default \
    CLOUD_NATIVE_SECRET_KUBERNETES_SECRET=jans \
    CLOUD_NATIVE_SECRET_KUBERNETES_USE_KUBE_CONFIG=false

# ===========
# Generic ENV
# ===========

ENV CLOUD_NATIVE_WAIT_MAX_TIME=300 \
    CLOUD_NATIVE_WAIT_SLEEP_DURATION=10 \
    CLOUD_NATIVE_NAMESPACE=jans

# ====
# misc
# ====

LABEL name="configuration-manager" \
    maintainer="Janssen <support@jans.io>" \
    vendor="Janssen" \
    version="5.0.0" \
    release="dev" \
    summary="Janssen Configuration Manager" \
    description="Manage config and secret"

COPY scripts /app/scripts
RUN mkdir -p /etc/certs /app/db \
    && chmod +x /app/scripts/entrypoint.sh

ENTRYPOINT ["tini", "-g", "--", "sh", "/app/scripts/entrypoint.sh"]
CMD ["--help"]
