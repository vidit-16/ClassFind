#!/bin/bash
# Serve the app over HTTPS with a Let's Encrypt certificate.
#
# Runs after every deploy and configuration change, and every hour from a
# systemd timer. The timer is what makes it recover on its own: when Elastic
# Beanstalk replaces the instance, the new one starts without a certificate,
# and if the deploy-time attempt fails, the next hourly run tries again. On an
# instance that already has a certificate, the hourly run only renews it when it
# is close to expiry. If anything fails, the app stays up on HTTP and the deploy
# is not failed.

set -uo pipefail

DOMAIN=$(/opt/elasticbeanstalk/bin/get-config environment -k CLASSFIND_DOMAIN 2>/dev/null || true)
DOMAIN=${DOMAIN:-classfind-prod.eba-ttyqcasp.ap-south-1.elasticbeanstalk.com}
WEBROOT=/var/www/letsencrypt
CERTBOT=/opt/certbot/bin/certbot
LIVE=/etc/letsencrypt/live/$DOMAIN
SCRIPT=$(readlink -f "$0")

skip() {
    echo "https: $1; serving HTTP only, will retry within the hour" >&2
    exit 0
}

# Install the timer first, so a failure further down is retried rather than
# left until the next deploy.
cat > /etc/systemd/system/classfind-https.service <<UNIT
[Unit]
Description=Obtain or renew the Let's Encrypt certificate and serve HTTPS

[Service]
Type=oneshot
ExecStart=/bin/bash $SCRIPT
UNIT

cat > /etc/systemd/system/classfind-https.timer <<'UNIT'
[Unit]
Description=Keep HTTPS working: retry the certificate hourly and renew it before expiry

[Timer]
OnBootSec=5min
OnCalendar=hourly
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# The renewal-only timer from the first version is replaced by the one above.
if [ -f /etc/systemd/system/certbot-renew.timer ]; then
    systemctl disable --now certbot-renew.timer 2>/dev/null
    rm -f /etc/systemd/system/certbot-renew.timer /etc/systemd/system/certbot-renew.service
fi
systemctl daemon-reload
systemctl enable classfind-https.timer
systemctl start classfind-https.timer

mkdir -p "$WEBROOT"

# certbot 5 drops Python 3.9, the system Python on Amazon Linux 2023.
if [ ! -x "$CERTBOT" ]; then
    python3 -m venv /opt/certbot && /opt/certbot/bin/pip install --quiet "certbot<5" \
        || skip "could not install certbot"
fi

if [ -f "$LIVE/fullchain.pem" ]; then
    # Does nothing until the certificate is within 30 days of expiry.
    "$CERTBOT" renew --quiet || echo "https: renewal failed; the current certificate is still in use" >&2
else
    # nginx can take a moment to serve the challenge folder on a fresh
    # instance, so try a few times before leaving it to the timer.
    for attempt in 1 2 3; do
        "$CERTBOT" certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
            --non-interactive --agree-tos --register-unsafely-without-email && break
        [ "$attempt" = 3 ] && skip "could not obtain a certificate for $DOMAIN"
        sleep 20
    done
fi

cat > /etc/nginx/conf.d/https.conf <<CONF
server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate     $LIVE/fullchain.pem;
    ssl_certificate_key $LIVE/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }
}
CONF

if ! nginx -t; then
    rm -f /etc/nginx/conf.d/https.conf
    skip "nginx rejected the HTTPS configuration"
fi
systemctl reload nginx

echo "https: serving $DOMAIN on port 443"
