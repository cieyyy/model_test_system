#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
address=${1:?Usage: bash deploy/install.sh SERVER_IP}
[[ $address =~ ^[0-9.]+$ ]] || { echo 'Provide an IPv4 address.' >&2; exit 1; }
base=/opt/model-test
repo=$base/repository
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv nginx apache2-utils git curl procps rsync openssl
id model-test >/dev/null 2>&1 || useradd --system --home /var/lib/model-test --shell /usr/sbin/nologin model-test
install -d -m 755 "$base" "$base/releases" /etc/model-test /var/www/model-test-acme
install -d -m 700 /var/backups/model-test
install -d -m 700 -o model-test -g model-test /var/lib/model-test
for directory in data media results tiktoken acceptance-fixtures; do
    install -d -m 700 -o model-test -g model-test "/var/lib/model-test/$directory"
done
if [[ ! -f /etc/model-test/platform.env ]]; then
    sed "s/YOUR_SERVER_IP/$address/" "$source_dir/platform.env.example" > /etc/model-test/platform.env
    chmod 600 /etc/model-test/platform.env
fi
if [[ ! -f /etc/model-test/htpasswd ]]; then
    password=$(openssl rand -base64 24)
    printf '%s\n' "$password" | htpasswd -ciB /etc/model-test/htpasswd admin
    printf 'URL: https://%s\nUsername: admin\nPassword: %s\n' "$address" "$password" > /root/model-test-login.txt
    chmod 600 /root/model-test-login.txt
    unset password
fi
chown root:www-data /etc/model-test/htpasswd
chmod 640 /etc/model-test/htpasswd
install -m 644 "$source_dir/model-test.service" /etc/systemd/system/model-test.service
install -m 755 "$source_dir/update.sh" /usr/local/bin/model-test-update
systemctl daemon-reload
systemctl enable model-test
if [[ ! -d $repo/.git ]]; then
    git clone https://github.com/cieyyy/model_test_system.git "$repo"
fi

# Bootstrap HTTP challenge before requesting an IP certificate.
if [[ ! -f /etc/letsencrypt/live/model-test/fullchain.pem ]]; then
    install -m 644 "$source_dir/nginx-http.conf" /etc/nginx/sites-available/model-test
    ln -sfn /etc/nginx/sites-available/model-test /etc/nginx/sites-enabled/model-test
    [[ ! -L /etc/nginx/sites-enabled/default ]] || unlink /etc/nginx/sites-enabled/default
    nginx -t
    systemctl reload nginx
    python3 -m venv /opt/model-test-certbot
    /opt/model-test-certbot/bin/pip install -q 'certbot==5.8.0'
    /opt/model-test-certbot/bin/certbot certonly --webroot -w /var/www/model-test-acme \
        --ip-address "$address" --required-profile shortlived --cert-name model-test \
        --non-interactive --agree-tos --register-unsafely-without-email
fi
sed "s/SERVER_ADDRESS/$address/g" "$source_dir/nginx.conf.example" > /etc/nginx/sites-available/model-test
ln -sfn /etc/nginx/sites-available/model-test /etc/nginx/sites-enabled/model-test
[[ ! -L /etc/nginx/sites-enabled/default ]] || unlink /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
install -m 644 "$source_dir/model-test-certbot.service" /etc/systemd/system/model-test-certbot.service
install -m 644 "$source_dir/model-test-certbot.timer" /etc/systemd/system/model-test-certbot.timer
systemctl daemon-reload
systemctl enable --now model-test-certbot.timer
/usr/local/bin/model-test-update
echo 'Installed. Initial login details: /root/model-test-login.txt'
