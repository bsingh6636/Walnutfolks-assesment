# AWS EC2 deployment with Docker and Nginx

These commands target a **fresh Ubuntu Server 24.04 LTS EC2 instance**.
Run the local steps in Windows PowerShell and the server steps in the SSH
terminal. Replace the uppercase placeholders before running commands.
Amazon Linux uses different package commands; this guide is for Ubuntu.

```text
Internet -> EC2 Nginx :80/:443 -> 127.0.0.1:8000 -> Docker / FastAPI
                                                       |
                                                       v
                                               Hosted MySQL 8
```

This setup uses your existing hosted MySQL database (for example Aiven).
Nginx runs on the host and the API plus its background worker run in one
container. `render.yaml` is specific to Render and is not used on EC2.

## 1. Create the EC2 server

In AWS Console -> EC2 -> Launch instance:

- Select **Ubuntu Server 24.04 LTS**, 64-bit x86.
- For a small assessment deployment, start with **t3.small** (2 GiB RAM)
  and 20 GiB gp3 storage. This is a starting size, not a capacity guarantee.
- Use a region near the database; the README describes Singapore.
- Create/download a `.pem` key pair.
- Use a public subnet with a route to an internet gateway and assign a
  public IPv4 address. Associate an Elastic IP if you need a stable address
  for DNS or a database allowlist.

Set these inbound rules on the instance's security group:

| Type | Port | Source |
| --- | --- | --- |
| SSH | 22 | My IP (your public IP with `/32`) |
| HTTP | 80 | `0.0.0.0/0` |
| HTTPS | 443 | `0.0.0.0/0` |

Keep the default outbound access for installation and the hosted database.
Do not add inbound rules for 8000 or MySQL: Nginx is the public entry point
and the database is external. These follow the
[AWS web server and SSH security group guidance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/security-group-rules-reference.html).

## 2. Push the deployment files from your PC

The files must be in your remote Git repository before the server clones it.
In PowerShell, from this repo:

```powershell
git add Dockerfile .dockerignore compose.yaml deploy/nginx.conf docs/AWS-DEPLOYMENT.md README.md
git diff --cached --stat
git commit -m "Add EC2 Docker and Nginx deployment"
git push -u origin HEAD
git branch --show-current
```

Use the branch printed by the last command as `YOUR_BRANCH` below. `.env`
stays on your machines and is already ignored by Git. Review any previously
staged changes before committing.

## 3. Connect from Windows

In PowerShell, replace the key path and public IP:

```powershell
ssh -i "C:\Users\bsing\Downloads\YOUR_KEY.pem" ubuntu@YOUR_EC2_PUBLIC_IP
```

If Windows OpenSSH reports that the private key permissions are too open,
restrict that specific key file and retry:

```powershell
$ec2KeyPath = "C:\Users\bsing\Downloads\YOUR_KEY.pem"
$ec2KeyOwner = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
icacls.exe $ec2KeyPath /reset
icacls.exe $ec2KeyPath /grant:r "${ec2KeyOwner}:(R)"
icacls.exe $ec2KeyPath /inheritance:r
ssh -i $ec2KeyPath ubuntu@YOUR_EC2_PUBLIC_IP
```

All remaining command blocks run **on EC2**, after SSH connects.

## 4. Install Docker, Compose, Git, and Nginx

Confirm the OS first:

```bash
cat /etc/os-release
```

Continue when it says Ubuntu 24.04. The installation below uses
[Docker's official Ubuntu package repository](https://docs.docker.com/engine/install/ubuntu/).
It assumes a fresh server without another Docker installation.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git nginx nano
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker nginx
sudo docker version
sudo docker compose version
sudo docker run --rm hello-world
```

If you have already enabled UFW on this server, also allow `OpenSSH` and
`Nginx Full` in UFW. The fresh-server path here uses the AWS security group.

## 5. Clone and configure the app

Replace `YOUR_REPOSITORY_URL` with your clone URL and `YOUR_BRANCH` with
the branch you pushed in step 2. A private repo needs an authorized Git
credential or SSH deploy key on the server.

```bash
mkdir -p ~/apps
cd ~/apps
git clone --branch YOUR_BRANCH YOUR_REPOSITORY_URL walnutfolks-assesment
cd ~/apps/walnutfolks-assesment
test -f .env || (umask 077; cp .env.example .env)
chmod 600 .env
nano .env
```

Fill in your database provider's actual values. For example:

```dotenv
DB_HOST=YOUR_MYSQL_HOST
DB_PORT=YOUR_MYSQL_PORT
DB_USER=YOUR_MYSQL_USER
DB_PASSWORD='YOUR_MYSQL_PASSWORD'
DB_NAME=YOUR_EXISTING_DATABASE
DB_SSL=true
PROCESSING_DELAY_SECONDS=30
RUN_WORKER_IN_API=true
```

In nano, save with **Ctrl+O**, Enter, then exit with **Ctrl+X**.

- Use the provider's hostname, not `localhost`: inside Docker, `localhost`
  refers to the API container.
- Use the provider's port; hosted MySQL may not use 3306.
- The database itself must already exist. The app creates its table on
  startup, so the database user needs table creation and read/write access.
- If your database has an IP allowlist, add the EC2 server's outbound public
  IP (the Elastic IP for this direct internet setup).
- Keep passwords single-quoted so Compose preserves dollar signs. An embedded
  single quote can be escaped as `\'`, for example `DB_PASSWORD='it\'s$secret'`.
  See [Compose environment syntax](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax).

## 6. Build and start the Docker container

From the repo directory:

```bash
sudo docker compose config --quiet
sudo docker compose up -d --build --wait --wait-timeout 180
sudo docker compose ps
sudo docker compose logs --tail=100 api
curl --fail-with-body http://127.0.0.1:8000/
```

Expect `{"status":"HEALTHY",...}`. Fix any container or database errors
before continuing. `--wait` checks the container's HTTP health check.
The `/` endpoint checks that the API responds; it does not continuously
check the database or the background worker. An unhealthy container is
reported as unhealthy; Docker does not restart it solely for that status.

Compose publishes port 8000 only on the host's loopback address, rotates
container logs, and restarts the service after a crash or server reboot
unless you deliberately stopped it.

## 7. Enable the Nginx reverse proxy

These commands install the included
[Nginx reverse proxy configuration](https://docs.nginx.com/nginx/admin-guide/web-server/reverse-proxy/).
They replace the Ubuntu default welcome site on this fresh server.

```bash
sudo install -m 0644 deploy/nginx.conf /etc/nginx/sites-available/transaction-webhooks
sudo ln -sfn /etc/nginx/sites-available/transaction-webhooks /etc/nginx/sites-enabled/transaction-webhooks
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
curl --fail-with-body http://127.0.0.1/
```

Open `http://YOUR_EC2_PUBLIC_IP/` and `http://YOUR_EC2_PUBLIC_IP/docs`
in your browser. The health JSON and FastAPI documentation should load.

## 8. Add HTTPS when you have a domain

Create an **A record** such as `api.example.com` pointing to the server's
public IP, preferably its Elastic IP. Wait until the record resolves to
that IP. Only add an AAAA record if the server also has working public IPv6.
Ports 80 and 443 must be reachable.

Edit the installed Nginx config:

```bash
sudo nano /etc/nginx/sites-available/transaction-webhooks
```

Replace `server_name _;` with `server_name api.example.com;`, using your
actual domain, then save. Install Certbot using its
[official Nginx instructions](https://certbot.eff.org/instructions?ws=nginx&os=snap):

```bash
sudo nginx -t && sudo systemctl reload nginx
sudo apt-get install -y snapd
sudo snap install --classic certbot
sudo /snap/bin/certbot --nginx --redirect -d api.example.com
sudo /snap/bin/certbot renew --dry-run
curl --fail-with-body https://api.example.com/
```

Replace `api.example.com` in the commands. Complete Certbot's email and terms
prompts. It installs the certificate and HTTP-to-HTTPS redirect and schedules
automatic renewal. Keep port 80 available for this renewal method.

Do not recopy `deploy/nginx.conf` over the installed config after this step:
Certbot has added the TLS settings to the installed file.

## 9. Check a transaction through Nginx

Use your HTTPS URL, or the HTTP public IP for an initial smoke test:

```bash
API_BASE='https://api.example.com'
TXN_ID="txn_ec2_$(date +%s)"
curl --fail-with-body -i -X POST "$API_BASE/v1/webhooks/transactions" \
  -H 'Content-Type: application/json' \
  -d "{\"transaction_id\":\"$TXN_ID\",\"source_account\":\"acc_test_user\",\"destination_account\":\"acc_test_merchant\",\"amount\":1500,\"currency\":\"INR\"}"

curl --fail-with-body "$API_BASE/v1/transactions/$TXN_ID"
sleep 35
curl --fail-with-body "$API_BASE/v1/transactions/$TXN_ID"
```

Expect HTTP `202`, then `PROCESSING`, then `PROCESSED` after roughly 30 seconds.
This creates a test transaction in the configured database. Repeating the
same POST with the same `TXN_ID` should still return 202 without processing
that transaction again.

## 10. Deploy future changes

Commit and push your changes from your PC. Then, on EC2:

```bash
cd ~/apps/walnutfolks-assesment
git pull --ff-only
sudo docker compose config --quiet
sudo docker compose up -d --build --wait --wait-timeout 180
curl --fail-with-body http://127.0.0.1:8000/
```

There is a brief interruption while the single container is replaced. MySQL
data stays in the hosted database. If you only changed `.env`, recreate the
container with `sudo docker compose up -d --force-recreate --wait --wait-timeout 180`.
A plain `restart` does not reload changed environment variables.

## Operations and troubleshooting

Run Compose commands from `~/apps/walnutfolks-assesment`:

```bash
sudo docker compose ps
sudo docker compose logs -f --tail=100 api
```

Press **Ctrl+C** to stop following logs; the service keeps running.

```bash
sudo systemctl status nginx --no-pager
sudo nginx -t
sudo tail -n 100 /var/log/nginx/error.log
sudo ss -lntp
df -h
```

| Symptom | What to check |
| --- | --- |
| `502 Bad Gateway` | Check container logs and `curl http://127.0.0.1:8000/`. The app may have failed to connect to MySQL. |
| Public URL times out but local curl works | Check the EC2 security group, public IP, subnet route, and any host firewall. |
| MySQL timeout or access denied | Check host, port, credentials, database name, and the provider's IP allowlist. |
| Container health check fails | Read `sudo docker compose logs --tail=100 api`; health checks cannot pass until app startup succeeds. |
| Certbot fails | Check DNS, public port 80, and the Nginx `server_name`. |

To deliberately stop the API:

```bash
sudo docker compose stop
```

To start it again:

```bash
sudo docker compose up -d --wait --wait-timeout 180
```

This deploys the assessment app as it currently exists. Before handling real
payment data, add webhook authentication and MySQL server certificate
verification: the current code accepts unsigned webhooks and encrypts the
database connection without verifying the server certificate.
