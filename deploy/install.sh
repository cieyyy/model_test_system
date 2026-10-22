#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
[[ $EUID == 0 ]] || { echo 'Run as root.' >&2; exit 1; }
address=${1:?Usage: bash deploy/install.sh SERVER_IP}
[[ $address =~ ^[0-9.]+$ ]] || { echo 'Provide an IPv4 address.' >&2; exit 1; }
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 git curl python3 rsync apache2-utils openssl
systemctl enable --now docker
id model-test >/dev/null 2>&1 || useradd --system --home /var/lib/model-test --shell /usr/sbin/nologin model-test
install -d -m 755 /opt/model-test/releases /etc/model-test /var/www/model-test-acme
install -d -m 700 /var/backups/model-test
install -d -m 700 -o model-test -g model-test /var/lib/model-test
for directory in data media results tiktoken acceptance-fixtures; do
    install -d -m 700 -o model-test -g model-test "/var/lib/model-test/$directory"
done
if [[ ! -f /etc/model-test/platform.env ]]; then
    sed "s/YOUR_SERVER_IP/$address/" "$source_dir/platform.env.example" > /etc/model-test/platform.env
fi
if [[ ! -f /etc/model-test/htpasswd ]]; then
    password=$(openssl rand -base64 24)
    printf '%s\n' "$password" | htpasswd -ciB /etc/model-test/htpasswd admin
    printf 'URL: https://%s\nUsername: admin\nPassword: %s\n' "$address" "$password" > /root/model-test-login.txt
    unset password
fi
chmod 600 /etc/model-test/platform.env
[[ ! -f /root/model-test-login.txt ]] || chmod 600 /root/model-test-login.txt
if [[ ! -d /opt/model-test/repository/.git ]]; then
    git clone https://github.com/cieyyy/model_test_system.git /opt/model-test/repository
fi

# Only a fresh installation needs an HTTP-only certificate bootstrap.
if [[ ! -f /etc/letsencrypt/live/model-test/fullchain.pem ]]; then
    if ss -ltn '( sport = :80 or sport = :443 )' | tail -n +2 | read -r _; then
        echo 'Ports 80/443 are occupied. Configure the existing site before installation.' >&2
        exit 1
    fi
    install -d -m 755 /etc/letsencrypt
    docker run -d --name model-test-acme-bootstrap --restart=no -p 80:80 \
        -v "$source_dir/nginx-http.conf:/etc/nginx/conf.d/default.conf:ro" \
        -v /var/www/model-test-acme:/var/www/model-test-acme:ro nginx:1.28-alpine
    trap 'docker rm -f model-test-acme-bootstrap >/dev/null 2>&1 || true' EXIT
    docker run --rm -v /etc/letsencrypt:/etc/letsencrypt \
        -v /var/www/model-test-acme:/var/www/model-test-acme \
        certbot/certbot:v5.8.0 certonly --webroot -w /var/www/model-test-acme \
        --ip-address "$address" --required-profile shortlived --cert-name model-test \
        --non-interactive --agree-tos --register-unsafely-without-email
    docker rm -f model-test-acme-bootstrap >/dev/null
    trap - EXIT
fi
install -m 755 "$source_dir/update.sh" /usr/local/bin/model-test-update
install -m 755 "$source_dir/ctl.sh" /usr/local/bin/model-test-ctl
/usr/local/bin/model-test-update
echo 'Docker installation complete. Login details: /root/model-test-login.txt'
