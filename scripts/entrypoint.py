import json
import logging.config
import os
import random
import socket
import time
import uuid

import click

from jans.pycloudlib import get_manager
from jans.pycloudlib import wait_for
from jans.pycloudlib.utils import get_random_chars
from jans.pycloudlib.utils import get_sys_random_chars
from jans.pycloudlib.utils import encode_text
from jans.pycloudlib.utils import exec_cmd
from jans.pycloudlib.utils import generate_base64_contents
from jans.pycloudlib.utils import safe_render
from jans.pycloudlib.utils import ldap_encode
from jans.pycloudlib.utils import get_server_certificate

from parameter import params_from_file
from settings import LOGGING_CONFIG

DEFAULT_SIG_KEYS = "RS256 RS384 RS512 ES256 ES384 ES512"
DEFAULT_ENC_KEYS = DEFAULT_SIG_KEYS

DEFAULT_CONFIG_FILE = "/app/db/config.json"
DEFAULT_SECRET_FILE = "/app/db/secret.json"
DEFAULT_GENERATE_FILE = "/app/db/generate.json"

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("configuration-manager")

manager = get_manager()


def encode_template(fn, ctx, base_dir="/app/templates"):
    path = os.path.join(base_dir, fn)
    # ctx is nested which has `config` and `secret` keys
    data = {}
    for _, v in ctx.items():
        data.update(v)
    with open(path) as f:
        return generate_base64_contents(safe_render(f.read(), data))


def generate_openid_keys(passwd, jks_path, jwks_path, dn, exp=365, sig_keys=DEFAULT_SIG_KEYS, enc_keys=DEFAULT_ENC_KEYS):
    cmd = " ".join([
        "java",
        "-Dlog4j.defaultInitOverride=true",
        "-cp /app/javalibs/*",
        "io.jans.as.client.util.KeyGenerator",
        "-enc_keys", enc_keys,
        "-sig_keys", sig_keys,
        "-dnname", "{!r}".format(dn),
        "-expiration", "{}".format(exp),
        "-keystore", jks_path,
        "-keypasswd", passwd,
    ])
    out, err, retcode = exec_cmd(cmd)
    if retcode == 0:
        with open(jwks_path, "w") as f:
            f.write(out.decode())
    return out, err, retcode


def export_openid_keys(keystore, keypasswd, alias, export_file):
    cmd = " ".join([
        "java",
        "-Dlog4j.defaultInitOverride=true",
        "-cp /app/javalibs/*",
        "io.jans.as.client.util.KeyExporter",
        "-keystore {}".format(keystore),
        "-keypasswd {}".format(keypasswd),
        "-alias {}".format(alias),
        "-exportfile {}".format(export_file),
    ])
    return exec_cmd(cmd)


def generate_pkcs12(suffix, passwd, hostname):
    # Convert key to pkcs12
    cmd = " ".join([
        "openssl",
        "pkcs12",
        "-export",
        "-inkey /etc/certs/{}.key".format(suffix),
        "-in /etc/certs/{}.crt".format(suffix),
        "-out /etc/certs/{}.pkcs12".format(suffix),
        "-name {}".format(hostname),
        "-passout pass:{}".format(passwd),
    ])
    _, err, retcode = exec_cmd(cmd)
    assert retcode == 0, "Failed to generate PKCS12 file; reason={}".format(err)


class CtxManager:
    def __init__(self, manager):
        self.manager = manager
        self.ctx = {"config": {}, "secret": {}}

    def set_config(self, key, value):
        logger.info(f"adding config {key}")
        self.ctx["config"][key] = value
        return value

    def set_secret(self, key, value):
        logger.info(f"adding secret {key}")
        self.ctx["secret"][key] = value
        return value

    def get_config(self, key, default=None):
        return self.ctx["config"].get(key) or default

    def get_secret(self, key, default=None):
        return self.ctx["secret"].get(key) or default


class CtxGenerator:
    def __init__(self, manager, params):
        self.params = params
        self.manager = manager
        self.ctx_manager = CtxManager(self.manager)

    @property
    def ctx(self):
        return self.ctx_manager.ctx

    def set_config(self, key, value):
        return self.ctx_manager.set_config(key, value)

    def set_secret(self, key, value):
        return self.ctx_manager.set_secret(key, value)

    def get_config(self, key, default=None):
        return self.ctx_manager.get_config(key, default)

    def get_secret(self, key, default=None):
        return self.ctx_manager.get_secret(key, default)

    def base_ctx(self):
        self.set_secret("encoded_salt", get_random_chars(24))
        self.set_config("orgName", self.params["org_name"])
        self.set_config("country_code", self.params["country_code"])
        self.set_config("state", self.params["state"])
        self.set_config("city", self.params["city"])
        self.set_config("hostname", self.params["hostname"])
        self.set_config("admin_email", self.params["email"])
        self.set_config("default_openid_jks_dn_name", "CN=oxAuth CA Certificates")
        self.set_secret("pairwiseCalculationKey", get_sys_random_chars(random.randint(20, 30)))
        self.set_secret("pairwiseCalculationSalt", get_sys_random_chars(random.randint(20, 30)))
        self.set_config("jetty_base", "/opt/jans/jetty")
        self.set_config("fido2ConfigFolder", "/etc/jans/conf/fido2")
        self.set_config("admin_inum", "{}".format(uuid.uuid4()))
        self.set_secret("encoded_oxtrust_admin_password", ldap_encode(self.params["admin_pw"]))

    def ldap_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")

        # self.set_secret("encoded_ldap_pw", ldap_encode(self.params["admin_pw"]))
        self.set_secret(
            "encoded_ox_ldap_pw",
            encode_text(self.params["ldap_pw"], encoded_salt),
        )
        self.set_config("ldap_init_host", "localhost")
        self.set_config("ldap_init_port", 1636)
        self.set_config("ldap_port", 1389)
        self.set_config("ldaps_port", 1636)
        self.set_config("ldap_binddn", "cn=directory manager")
        self.set_config("ldap_site_binddn", "cn=directory manager")
        ldap_truststore_pass = self.set_secret("ldap_truststore_pass", get_random_chars())
        self.set_config("ldapTrustStoreFn", "/etc/certs/opendj.pkcs12")
        hostname = self.get_config("hostname")

        generate_ssl_certkey(
            "opendj",
            self.get_secret("ldap_truststore_pass"),
            self.get_config("admin_email"),
            hostname,
            self.get_config("orgName"),
            self.get_config("country_code"),
            self.get_config("state"),
            self.get_config("city"),
        )
        with open("/etc/certs/opendj.pem", "w") as fw:
            with open("/etc/certs/opendj.crt") as fr:
                ldap_ssl_cert = fr.read()
                self.set_secret(
                    "ldap_ssl_cert",
                    encode_text(ldap_ssl_cert, encoded_salt),
                )

            with open("/etc/certs/opendj.key") as fr:
                ldap_ssl_key = fr.read()
                self.set_secret(
                    "ldap_ssl_key",
                    encode_text(ldap_ssl_key, encoded_salt),
                )

            ldap_ssl_cacert = "".join([ldap_ssl_cert, ldap_ssl_key])
            fw.write(ldap_ssl_cacert)
            self.set_secret(
                "ldap_ssl_cacert",
                encode_text(ldap_ssl_cacert, encoded_salt),
            )

        generate_pkcs12("opendj", ldap_truststore_pass, hostname)

        with open(self.get_config("ldapTrustStoreFn"), "rb") as fr:
            self.set_secret(
                "ldap_pkcs12_base64",
                encode_text(fr.read(), encoded_salt),
            )

        self.set_secret(
            "encoded_ldapTrustStorePass",
            encode_text(ldap_truststore_pass, encoded_salt),
        )

    def redis_ctx(self):
        self.set_secret("redis_pw", self.params.get("redis_pw", ""))

    def oxauth_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        self.set_config("oxauth_client_id", "1001.{}".format(uuid.uuid4()))
        self.set_secret(
            "oxauthClient_encoded_pw",
            encode_text(get_random_chars(), encoded_salt),
        )
        oxauth_openid_jks_fn = self.set_config("oxauth_openid_jks_fn", "/etc/certs/oxauth-keys.jks")
        self.set_secret("oxauth_openid_jks_pass", get_random_chars())
        oxauth_openid_jwks_fn = self.set_config("oxauth_openid_jwks_fn", "/etc/certs/oxauth-keys.json")
        self.set_config("oxauth_legacyIdTokenClaims", "false")
        self.set_config("oxauth_openidScopeBackwardCompatibility", "false")

        _, err, retcode = generate_openid_keys(
            self.get_secret("oxauth_openid_jks_pass"),
            self.get_config("oxauth_openid_jks_fn"),
            oxauth_openid_jwks_fn,
            self.get_config("default_openid_jks_dn_name"),
            exp=2,
            sig_keys="RS256 RS384 RS512 ES256 ES384 ES512 PS256 PS384 PS512",
            enc_keys="RSA1_5 RSA-OAEP",
        )
        if retcode != 0:
            logger.error(f"Unable to generate auth keys; reason={err}")
            raise click.Abort()

        basedir, fn = os.path.split(oxauth_openid_jwks_fn)
        self.set_secret("oxauth_openid_key_base64", encode_template(fn, self.ctx, basedir))

        # oxAuth keys
        self.set_config("oxauth_key_rotated_at", int(time.time()))

        with open(oxauth_openid_jks_fn, "rb") as fr:
            self.set_secret(
                "oxauth_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )

    def scim_rs_ctx(self):
        self.set_config("scim_rs_client_id", "1201.{}".format(uuid.uuid4()))
        scim_rs_client_jks_fn = self.set_config("scim_rs_client_jks_fn", "/etc/certs/scim-rs.jks")
        scim_rs_client_jwks_fn = self.set_config("scim_rs_client_jwks_fn", "/etc/certs/scim-rs-keys.json")
        scim_rs_client_jks_pass = self.set_secret("scim_rs_client_jks_pass", get_random_chars())
        encoded_salt = self.get_secret("encoded_salt")

        self.set_secret(
            "scim_rs_client_jks_pass_encoded",
            encode_text(scim_rs_client_jks_pass, encoded_salt),
        )

        out, err, retcode = generate_openid_keys(
            scim_rs_client_jks_pass,
            scim_rs_client_jks_fn,
            scim_rs_client_jwks_fn,
            self.get_config("default_openid_jks_dn_name"),
        )
        if retcode != 0:
            logger.error(f"Unable to generate SCIM RS keys; reason={err}")
            raise click.Abort()

        scim_rs_client_cert_alg = self.set_config("scim_rs_client_cert_alg", "RS512")

        cert_alias = ""
        for key in json.loads(out)["keys"]:
            if key["alg"] == scim_rs_client_cert_alg:
                cert_alias = key["kid"]
                break

        self.set_config("scim_rs_client_cert_alias", cert_alias)

        basedir, fn = os.path.split(scim_rs_client_jwks_fn)
        self.set_secret("scim_rs_client_base64_jwks", encode_template(fn, self.ctx, basedir))

        with open(scim_rs_client_jks_fn, "rb") as fr:
            self.set_secret(
                "scim_rs_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )

    def scim_rp_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        self.set_config("scim_rp_client_id", "1202.{}".format(uuid.uuid4()))
        scim_rp_client_jks_fn = self.set_config("scim_rp_client_jks_fn", "/etc/certs/scim-rp.jks")
        scim_rp_client_jwks_fn = self.set_config("scim_rp_client_jwks_fn", "/etc/certs/scim-rp-keys.json")
        scim_rp_client_jks_pass = self.set_secret("scim_rp_client_jks_pass", get_random_chars())
        self.set_secret(
            "scim_rp_client_jks_pass_encoded",
            encode_text(scim_rp_client_jks_pass, encoded_salt),
        )

        _, err, retcode = generate_openid_keys(
            scim_rp_client_jks_pass,
            scim_rp_client_jks_fn,
            scim_rp_client_jwks_fn,
            self.get_config("default_openid_jks_dn_name"),
        )
        if retcode != 0:
            logger.error(f"Unable to generate SCIM RP keys; reason={err}")
            raise click.Abort()

        basedir, fn = os.path.split(scim_rp_client_jwks_fn)
        self.set_secret("scim_rp_client_base64_jwks", encode_template(fn, self.ctx, basedir))

        with open(scim_rp_client_jks_fn, "rb") as fr:
            self.set_secret(
                "scim_rp_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )
        self.set_config("scim_resource_oxid", "1203.{}".format(uuid.uuid4()))

    def passport_rs_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        self.set_config("passport_rs_client_id", "1501.{}".format(uuid.uuid4()))
        passport_rs_client_jks_fn = self.set_config("passport_rs_client_jks_fn", "/etc/certs/passport-rs.jks")
        passport_rs_client_jwks_fn = self.set_config("passport_rs_client_jwks_fn", "/etc/certs/passport-rs-keys.json")
        passport_rs_client_jks_pass = self.set_secret("passport_rs_client_jks_pass", get_random_chars())
        self.set_secret(
            "passport_rs_client_jks_pass_encoded",
            encode_text(passport_rs_client_jks_pass, encoded_salt),
        )

        out, err, retcode = generate_openid_keys(
            passport_rs_client_jks_pass,
            passport_rs_client_jks_fn,
            passport_rs_client_jwks_fn,
            self.get_config("default_openid_jks_dn_name"),
        )
        if retcode != 0:
            logger.error("Unable to generate Passport RS keys; reason={}".format(err))
            raise click.Abort()

        passport_rs_client_cert_alg = self.set_config("passport_rs_client_cert_alg", "RS512")

        cert_alias = ""
        for key in json.loads(out)["keys"]:
            if key["alg"] == passport_rs_client_cert_alg:
                cert_alias = key["kid"]
                break

        self.set_config("passport_rs_client_cert_alias", cert_alias)

        basedir, fn = os.path.split(passport_rs_client_jwks_fn)
        self.set_secret(
            "passport_rs_client_base64_jwks",
            encode_template(fn, self.ctx, basedir),
        )

        with open(passport_rs_client_jks_fn, "rb") as fr:
            self.set_secret(
                "passport_rs_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )
        self.set_config("passport_resource_id", "1504.{}".format(uuid.uuid4()))

    def passport_rp_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        self.set_config("passport_rp_client_id", "1502.{}".format(uuid.uuid4()))
        self.set_config("passport_rp_ii_client_id", "1503.{}".format(uuid.uuid4()))
        passport_rp_client_jks_pass = self.set_secret("passport_rp_client_jks_pass", get_random_chars())
        passport_rp_client_jks_fn = self.set_config("passport_rp_client_jks_fn", "/etc/certs/passport-rp.jks")
        passport_rp_client_jwks_fn = self.set_config("passport_rp_client_jwks_fn", "/etc/certs/passport-rp-keys.json")
        passport_rp_client_cert_fn = self.set_config("passport_rp_client_cert_fn", "/etc/certs/passport-rp.pem")
        passport_rp_client_cert_alg = self.set_config("passport_rp_client_cert_alg", "RS512")

        out, err, code = generate_openid_keys(
            passport_rp_client_jks_pass,
            passport_rp_client_jks_fn,
            passport_rp_client_jwks_fn,
            self.get_config("default_openid_jks_dn_name"),
        )
        if code != 0:
            logger.error(f"Unable to generate Passport RP keys; reason={err}")
            raise click.Abort()

        cert_alias = ""
        for key in json.loads(out)["keys"]:
            if key["alg"] == passport_rp_client_cert_alg:
                cert_alias = key["kid"]
                break

        self.set_config("passport_rp_client_cert_alias", cert_alias)

        _, err, retcode = export_openid_keys(
            passport_rp_client_jks_fn,
            passport_rp_client_jks_pass,
            cert_alias,
            passport_rp_client_cert_fn,
        )
        if retcode != 0:
            logger.error(f"Unable to generate Passport RP client cert; reason={err}")
            raise click.Abort()

        basedir, fn = os.path.split(passport_rp_client_jwks_fn)
        self.set_secret("passport_rp_client_base64_jwks", encode_template(fn, self.ctx, basedir))

        with open(passport_rp_client_jks_fn, "rb") as fr:
            self.set_secret(
                "passport_rp_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )

        with open(passport_rp_client_cert_fn) as fr:
            self.set_secret(
                "passport_rp_client_cert_base64",
                encode_text(fr.read(), encoded_salt),
            )

    def passport_sp_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        passportSpKeyPass = self.set_secret("passportSpKeyPass", get_random_chars())  # noqa: N806
        self.set_config("passportSpTLSCACert", '/etc/certs/passport-sp.pem')
        passportSpTLSCert = self.set_config("passportSpTLSCert", '/etc/certs/passport-sp.crt')  # noqa: N806
        passportSpTLSKey = self.set_config("passportSpTLSKey", '/etc/certs/passport-sp.key')  # noqa: N806
        self.set_secret("passportSpJksPass", get_random_chars())
        self.set_config("passportSpJksFn", '/etc/certs/passport-sp.jks')

        generate_ssl_certkey(
            "passport-sp",
            passportSpKeyPass,
            self.get_config("admin_email"),
            self.get_config("hostname"),
            self.get_config("orgName"),
            self.get_config("country_code"),
            self.get_config("state"),
            self.get_config("city"),
        )
        with open(passportSpTLSCert) as f:
            self.set_secret(
                "passport_sp_cert_base64",
                encode_text(f.read(), encoded_salt),
            )

        with open(passportSpTLSKey) as f:
            self.set_secret(
                "passport_sp_key_base64",
                encode_text(f.read(), encoded_salt),
            )

    def web_ctx(self):
        ssl_cert = "/etc/certs/web_https.crt"
        ssl_key = "/etc/certs/web_https.key"
        ssl_cert_pass = self.set_secret("ssl_cert_pass", get_random_chars())

        # get cert and key (if available) with priorities below:
        #
        # 1. from mounted files
        # 2. from fronted (key file is an empty file)
        # 3. self-generate files

        ssl_cert_exists = os.path.isfile(ssl_cert)
        ssl_key_exists = os.path.isfile(ssl_key)
        hostname = self.get_config("hostname")

        logger.info(f"Resolving {ssl_cert} and {ssl_key}")

        # check from mounted files
        if not (ssl_cert_exists and ssl_key_exists):
            # no mounted files, hence download from frontend
            addr = os.environ.get("CN_INGRESS_ADDRESS") or hostname
            servername = os.environ.get("CN_INGRESS_SERVERNAME") or addr

            logger.warning(
                f"Unable to find mounted {ssl_cert} and {ssl_key}; "
                f"trying to download from {addr}:443 (servername {servername})"  # noqa: C812
            )
            try:
                # cert will be downloaded into `ssl_cert` path
                get_server_certificate(addr, 443, ssl_cert, servername)
                if not ssl_key_exists:
                    # since cert is downloaded, key must mounted
                    # or generate empty file
                    with open(ssl_key, "w") as f:
                        f.write("")
            except (socket.gaierror, socket.timeout, OSError) as exc:
                # address not resolved or timed out
                logger.warning(f"Unable to download cert; reason={exc}")
            finally:
                ssl_cert_exists = os.path.isfile(ssl_cert)
                ssl_key_exists = os.path.isfile(ssl_key)

        # no mounted nor downloaded files, hence we need to create self-generated files
        if not (ssl_cert_exists and ssl_key_exists):
            logger.info(f"Creating self-generated {ssl_cert} and {ssl_key}")
            generate_ssl_certkey(
                "web_https",
                ssl_cert_pass,
                self.get_config("admin_email"),
                hostname,
                self.get_config("orgName"),
                self.get_config("country_code"),
                self.get_config("state"),
                self.get_config("city"),
            )

        with open(ssl_cert) as f:
            self.set_secret("ssl_cert", f.read())

        with open(ssl_key) as f:
            self.set_secret("ssl_key", f.read())

    def oxshibboleth_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        hostname = self.get_config("hostname")
        admin_email = self.get_config("admin_email")
        orgName = self.get_config("orgName")  # noqa: N806
        country_code = self.get_config("country_code")
        state = self.get_config("state")
        city = self.get_config("city")
        self.set_config("idp_client_id", "1101.{}".format(uuid.uuid4()))
        self.set_secret(
            "idpClient_encoded_pw",
            encode_text(get_random_chars(), encoded_salt),
        )
        shibJksFn = self.set_config("shibJksFn", "/etc/certs/shibIDP.jks")  # noqa: N806
        shibJksPass = self.set_secret("shibJksPass", get_random_chars())  # noqa: N806
        self.set_secret(
            "encoded_shib_jks_pw",
            encode_text(shibJksPass, encoded_salt),
        )

        generate_ssl_certkey(
            "shibIDP",
            shibJksPass,
            admin_email,
            hostname,
            orgName,
            country_code,
            state,
            city,
        )

        generate_keystore("shibIDP", hostname, shibJksPass)

        with open("/etc/certs/shibIDP.crt") as f:
            self.set_secret(
                "shibIDP_cert",
                encode_text(f.read(), encoded_salt),
            )

        with open("/etc/certs/shibIDP.key") as f:
            self.set_secret(
                "shibIDP_key",
                encode_text(f.read(), encoded_salt),
            )

        with open(shibJksFn, "rb") as f:
            self.set_secret(
                "shibIDP_jks_base64",
                encode_text(f.read(), encoded_salt),
            )

        self.set_config("shibboleth_version", "v3")
        self.set_config("idp3Folder", "/opt/shibboleth-idp")

        idp3_signing_cert = "/etc/certs/idp-signing.crt"
        idp3_signing_key = "/etc/certs/idp-signing.key"

        generate_ssl_certkey(
            "idp-signing",
            shibJksPass,
            admin_email,
            hostname,
            orgName,
            country_code,
            state,
            city,
        )

        with open(idp3_signing_cert) as f:
            self.set_secret(
                "idp3SigningCertificateText", f.read())

        with open(idp3_signing_key) as f:
            self.set_secret(
                "idp3SigningKeyText", f.read())

        idp3_encryption_cert = "/etc/certs/idp-encryption.crt"
        idp3_encryption_key = "/etc/certs/idp-encryption.key"

        generate_ssl_certkey(
            "idp-encryption",
            shibJksPass,
            admin_email,
            hostname,
            orgName,
            country_code,
            state,
            city,
        )

        with open(idp3_encryption_cert) as f:
            self.set_secret("idp3EncryptionCertificateText", f.read())

        with open(idp3_encryption_key) as f:
            self.set_secret("idp3EncryptionKeyText", f.read())

        _, err, code = gen_idp3_key(shibJksPass)
        if code != 0:
            logger.warninging(f"Unable to generate Shibboleth sealer; reason={err}")
            raise click.Abort()

        with open("/etc/certs/sealer.jks", "rb") as f:
            self.set_secret(
                "sealer_jks_base64",
                encode_text(f.read(), encoded_salt),
            )

        with open("/etc/certs/sealer.kver") as f:
            self.set_secret(
                "sealer_kver_base64",
                encode_text(f.read(), encoded_salt),
            )

    def oxtrust_api_rs_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        api_rs_client_jks_fn = self.set_config("api_rs_client_jks_fn", "/etc/certs/api-rs.jks")
        api_rs_client_jwks_fn = self.set_config("api_rs_client_jwks_fn", "/etc/certs/api-rs-keys.json")
        api_rs_client_jks_pass = self.set_secret("api_rs_client_jks_pass", get_random_chars())
        self.set_secret(
            "api_rs_client_jks_pass_encoded",
            encode_text(api_rs_client_jks_pass, encoded_salt),
        )

        out, err, retcode = generate_openid_keys(
            api_rs_client_jks_pass,
            api_rs_client_jks_fn,
            api_rs_client_jwks_fn,
            self.get_config("default_openid_jks_dn_name"),
        )
        if retcode != 0:
            logger.error(f"Unable to generate oxTrust API RS keys; reason={err}")
            raise click.Abort()

        api_rs_client_cert_alg = self.set_config("api_rs_client_cert_alg", "RS512")

        cert_alias = ""
        for key in json.loads(out)["keys"]:
            if key["alg"] == api_rs_client_cert_alg:
                cert_alias = key["kid"]
                break
        self.set_config("api_rs_client_cert_alias", cert_alias)

        basedir, fn = os.path.split(api_rs_client_jwks_fn)
        self.set_secret("api_rs_client_base64_jwks", encode_template(fn, self.ctx, basedir))

        self.set_config("oxtrust_resource_server_client_id", '1401.{}'.format(uuid.uuid4()))
        self.set_config("oxtrust_resource_id", '1403.{}'.format(uuid.uuid4()))

        with open(api_rs_client_jks_fn, "rb") as fr:
            self.set_secret(
                "api_rs_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )

    def oxtrust_api_rp_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        api_rp_client_jks_fn = self.set_config("api_rp_client_jks_fn", "/etc/certs/api-rp.jks")
        api_rp_client_jwks_fn = self.set_config("api_rp_client_jwks_fn", "/etc/certs/api-rp-keys.json")
        api_rp_client_jks_pass = self.set_secret("api_rp_client_jks_pass", get_random_chars())
        self.set_secret(
            "api_rp_client_jks_pass_encoded",
            encode_text(api_rp_client_jks_pass, encoded_salt),
        )
        _, err, retcode = generate_openid_keys(
            api_rp_client_jks_pass,
            api_rp_client_jks_fn,
            api_rp_client_jwks_fn,
            self.get_config("default_openid_jks_dn_name"),
        )
        if retcode != 0:
            logger.error(f"Unable to generate oxTrust API RP keys; reason={err}")
            raise click.Abort()

        basedir, fn = os.path.split(api_rp_client_jwks_fn)
        self.set_secret("api_rp_client_base64_jwks", encode_template(fn, self.ctx, basedir))

        self.set_config("oxtrust_requesting_party_client_id", '1402.{}'.format(uuid.uuid4()))

        with open(api_rp_client_jks_fn, "rb") as fr:
            self.set_secret(
                "api_rp_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )

    def oxtrust_api_client_ctx(self):
        self.set_config("api_test_client_id", "0008-{}".format(uuid.uuid4()))
        self.set_secret("api_test_client_secret", get_random_chars(24))

    def radius_ctx(self):
        encoded_salt = self.get_secret("encoded_salt")
        namespace = os.environ.get("CN_NAMESPACE", "jans")
        self.set_config(f"{namespace}_radius_client_id", '1701.{}'.format(uuid.uuid4()))
        self.set_secret(
            f"{namespace}_ro_encoded_pw",
            encode_text(get_random_chars(), encoded_salt),
        )

        radius_jwt_pass = get_random_chars()
        self.set_secret(
            "radius_jwt_pass",
            encode_text(radius_jwt_pass, encoded_salt),
        )

        out, err, code = generate_openid_keys(
            radius_jwt_pass,
            "/etc/certs/jans-radius.jks",
            "/etc/certs/jans-radius.keys",
            self.get_config("default_openid_jks_dn_name"),
        )
        if code != 0:
            logger.error(f"Unable to generate Radius keys; reason={err}")
            raise click.Abort()

        for key in json.loads(out)["keys"]:
            if key["alg"] == "RS512":
                self.set_config("radius_jwt_keyId", key["kid"])
                break

        with open("/etc/certs/jans-radius.jks", "rb") as fr:
            self.set_secret(
                "radius_jks_base64",
                encode_text(fr.read(), encoded_salt),
            )

        basedir, fn = os.path.split("/etc/certs/jans-radius.keys")
        self.set_secret(
            f"{namespace}_ro_client_base64_jwks",
            encode_template(fn, self.ctx, basedir),
        )

    def scim_client_ctx(self):
        self.set_config("scim_test_client_id", "0008-{}".format(uuid.uuid4()))
        self.set_secret("scim_test_client_secret", get_random_chars(24))

    def couchbase_ctx(self):
        self.set_config("couchbaseTrustStoreFn", "/etc/certs/couchbase.pkcs12")
        self.set_secret("couchbase_shib_user_password", get_random_chars())

    def jackrabbit_ctx(self):
        # self.set_secret("jca_pw", get_random_chars())
        # self.set_secret("jca_pw", "admin")
        pass

    def generate(self):
        self.base_ctx()
        self.web_ctx()
        self.ldap_ctx()
        self.redis_ctx()
        self.oxauth_ctx()
        self.scim_rs_ctx()
        self.scim_rp_ctx()
        # self.passport_rs_ctx()
        # self.passport_rp_ctx()
        # self.passport_sp_ctx()
        # self.oxshibboleth_ctx()
        # self.oxtrust_api_rs_ctx()
        # self.oxtrust_api_rp_ctx()
        # self.oxtrust_api_client_ctx()
        # self.radius_ctx()
        self.scim_client_ctx()
        self.couchbase_ctx()
        self.jackrabbit_ctx()

        # populated config
        return self.ctx


def generate_ssl_certkey(suffix, passwd, email, hostname, org_name,
                         country_code, state, city):
    # create key with password
    _, err, retcode = exec_cmd(" ".join([
        "openssl",
        "genrsa -des3",
        "-out /etc/certs/{}.key.orig".format(suffix),
        "-passout pass:'{}' 2048".format(passwd),
    ]))
    assert retcode == 0, "Failed to generate SSL key with password; reason={}".format(err)

    # create .key
    _, err, retcode = exec_cmd(" ".join([
        "openssl",
        "rsa",
        "-in /etc/certs/{}.key.orig".format(suffix),
        "-passin pass:'{}'".format(passwd),
        "-out /etc/certs/{}.key".format(suffix),
    ]))
    assert retcode == 0, "Failed to generate SSL key; reason={}".format(err)

    # create .csr
    _, err, retcode = exec_cmd(" ".join([
        "openssl",
        "req",
        "-new",
        "-key /etc/certs/{}.key".format(suffix),
        "-out /etc/certs/{}.csr".format(suffix),
        """-subj /C="{}"/ST="{}"/L="{}"/O="{}"/CN="{}"/emailAddress='{}'""".format(country_code, state, city, org_name, hostname, email),

    ]))
    assert retcode == 0, "Failed to generate SSL CSR; reason={}".format(err)

    # create .crt
    _, err, retcode = exec_cmd(" ".join([
        "openssl",
        "x509",
        "-req",
        "-days 365",
        "-in /etc/certs/{}.csr".format(suffix),
        "-signkey /etc/certs/{}.key".format(suffix),
        "-out /etc/certs/{}.crt".format(suffix),
    ]))
    assert retcode == 0, "Failed to generate SSL cert; reason={}".format(err)

    # return the paths
    return "/etc/certs/{}.crt".format(suffix), \
           "/etc/certs/{}.key".format(suffix)


def generate_keystore(suffix, hostname, keypasswd):
    # converts key to pkcs12
    cmd = " ".join([
        "openssl",
        "pkcs12",
        "-export",
        "-inkey /etc/certs/{}.key".format(suffix),
        "-in /etc/certs/{}.crt".format(suffix),
        "-out /etc/certs/{}.pkcs12".format(suffix),
        "-name {}".format(hostname),
        "-passout pass:'{}'".format(keypasswd),
    ])
    _, err, retcode = exec_cmd(cmd)
    assert retcode == 0, "Failed to generate PKCS12 keystore; reason={}".format(err)

    # imports p12 to keystore
    cmd = " ".join([
        "keytool",
        "-importkeystore",
        "-srckeystore /etc/certs/{}.pkcs12".format(suffix),
        "-srcstorepass {}".format(keypasswd),
        "-srcstoretype PKCS12",
        "-destkeystore /etc/certs/{}.jks".format(suffix),
        "-deststorepass {}".format(keypasswd),
        "-deststoretype JKS",
        "-keyalg RSA",
        "-noprompt",
    ])
    _, err, retcode = exec_cmd(cmd)
    assert retcode == 0, "Failed to generate JKS keystore; reason={}".format(err)


def gen_idp3_key(storepass):
    cmd = " ".join([
        "java",
        "-classpath '/app/javalibs/*'",
        "net.shibboleth.utilities.java.support.security.BasicKeystoreKeyStrategyTool",
        "--storefile /etc/certs/sealer.jks",
        "--versionfile /etc/certs/sealer.kver",
        "--alias secret",
        "--storepass {}".format(storepass),
    ])
    return exec_cmd(cmd)


def _save_generated_ctx(manager, data, type_):
    if type_ == "config":
        backend = manager.config
    else:
        backend = manager.secret

    logger.info("Saving {} to backend".format(type_))

    for k, v in data.items():
        backend.set(k, v)


def _load_from_file(manager, filepath, type_):
    ctx_manager = CtxManager(manager)
    if type_ == "config":
        setter = ctx_manager.set_config
        backend = manager.config
    else:
        setter = ctx_manager.set_secret
        backend = manager.secret

    logger.info(f"Loading {type_} from {filepath}")

    with open(filepath, "r") as f:
        data = json.loads(f.read())

    ctx = data.get(f"_{type_}")
    if not ctx:
        logger.warning(f"Missing '_{type_}' key")
        return

    # tolerancy before checking existing key
    time.sleep(5)

    for k, v in ctx.items():
        val = setter(k, v)
        backend.set(k, val)


def _dump_to_file(manager, filepath, type_):
    if type_ == "config":
        backend = manager.config
    else:
        backend = manager.secret

    logger.info("Saving {} to {}".format(type_, filepath))

    data = {"_{}".format(type_): backend.all()}
    data = json.dumps(data, sort_keys=True, indent=4)
    with open(filepath, "w") as f:
        f.write(data)

# ============
# CLI commands
# ============


@click.group()
def cli():
    pass


@cli.command()
@click.option(
    "--generate-file",
    type=click.Path(exists=False),
    help="Absolute path to file containing parameters for generating config and secret",
    default=DEFAULT_GENERATE_FILE,
    show_default=True,
)
@click.option(
    "--config-file",
    type=click.Path(exists=False),
    help="Absolute path to file contains config",
    default=DEFAULT_CONFIG_FILE,
    show_default=True,
)
@click.option(
    "--secret-file",
    type=click.Path(exists=False),
    help="Absolute path to file contains secret",
    default=DEFAULT_SECRET_FILE,
    show_default=True,
)
def load(generate_file, config_file, secret_file):
    """Loads config and secret from JSON files (generate if not exist).
    """
    deps = ["config_conn", "secret_conn"]
    wait_for(manager, deps=deps)

    # check whether config and secret in backend have been initialized
    if manager.config.get("hostname") and manager.secret.get("ssl_cert"):
        # config and secret may have been initialized
        logger.info("Config and secret have been initialized")
        return

    # there's no config and secret in backend, check whether to load from files
    if os.path.isfile(config_file) and os.path.isfile(secret_file):
        # load from existing files
        logger.info(f"Re-using config and secret from {config_file} and {secret_file}")
        _load_from_file(manager, config_file, "config")
        _load_from_file(manager, secret_file, "secret")
        return

    # no existing files, hence generate new config and secret from parameters
    logger.info(f"Loading parameters from {generate_file}")
    params, err, code = params_from_file(generate_file)
    if code != 0:
        logger.error(f"Unable to load parameters; reason={err}")
        raise click.Abort()

    logger.info("Generating new config and secret")
    ctx_generator = CtxGenerator(manager, params)
    ctx = ctx_generator.generate()

    # save config to its backend and file
    _save_generated_ctx(manager, ctx["config"], "config")
    _dump_to_file(manager, config_file, "config")

    # save secret to its backend and file
    _save_generated_ctx(manager, ctx["secret"], "secret")
    _dump_to_file(manager, secret_file, "secret")


@cli.command()
@click.option(
    "--config-file",
    type=click.Path(exists=False),
    help="Absolute path to file to save config",
    default=DEFAULT_CONFIG_FILE,
    show_default=True,
)
@click.option(
    "--secret-file",
    type=click.Path(exists=False),
    help="Absolute path to file to save secret",
    default=DEFAULT_SECRET_FILE,
    show_default=True,
)
def dump(config_file, secret_file):
    """Dumps config and secret into JSON files.
    """
    deps = ["config_conn", "secret_conn"]
    wait_for(manager, deps=deps)

    _dump_to_file(manager, config_file, "config")
    _dump_to_file(manager, secret_file, "secret")


if __name__ == "__main__":
    cli(prog_name="configuration-manager")
