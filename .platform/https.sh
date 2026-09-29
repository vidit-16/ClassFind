#!/bin/bash
# Serve the app over HTTPS with a Let's Encrypt certificate.
#
# Runs after every deploy and configuration change. The certificate is
# requested once per instance and renewed by a systemd timer. If anything here
# fails, the app stays up on HTTP and the deploy is not failed.

set -uo pipefail

DOMAIN=$(/opt/elasticbeanstalk/bin/get-config environment -k CLASSFIND_DOMAIN 2>/dev/null || true)
DOMAIN=${DOMAIN:-classfind-prod.eba-ttyqcasp.ap-south-1.elasticbeanstalk.com}
WEBROOT=/var/www/letsencrypt
CERTBOT=/opt/certbot/bin/certbot
LIVE=/etc/letsencrypt/live/$DOMAIN

skip() {
    echo "https: $1; serving HTTP only" >&2
    exit 0
}

mkdir -p "$WEBROOT"

# certbot 5 drops Python 3.9, the system Python on Amazon Linux 2023.
if [ ! -x "$CERTBOT" ]; then
    python3 -m venv /opt/certbot && /opt/certbot/bin/pip install --quiet "certbot<5" \
        || skip "could not install certbot"
fi

if [ ! -f "$LIVE/fullchain.pem" ]; then
    "$CERTBOT" certonly --webroot -w "$WEBROOT" -d "$DOMAIN" \
        --non-interactive --agree-tos --register-unsafely-without-email \
        || skip "could not obtain a certificate for $DOMAIN"
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

# Certificates last 90 days; renew twice a day once they are within 30.
cat > /etc/systemd/system/certbot-renew.service <<'UNIT'
[Unit]
Description=Renew the Let's Encrypt certificate

[Service]
Type=oneshot
ExecStart=/opt/certbot/bin/certbot renew --quiet --deploy-hook "systemctl reload nginx"
UNIT

cat > /etc/systemd/system/certbot-renew.timer <<'UNIT'
[Unit]
Description=Renew the Let's Encrypt certificate twice a day

[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now certbot-renew.timer

echo "https: serving $DOMAIN on port 443"
