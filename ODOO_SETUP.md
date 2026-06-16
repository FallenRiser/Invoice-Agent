# Odoo setup (self-hosted via Podman)

Run this yourself, locally. Total time: ~15–25 min, mostly unattended image pulls.

## 1. Make sure Podman is ready

```powershell
podman machine list
```

If you see no machine, or it's not "Currently running":

```powershell
podman machine init
podman machine start
```

## 2. Network + containers

```powershell
podman network create odoo-net

podman run -d --name odoo-db --network odoo-net `
  -e POSTGRES_USER=odoo -e POSTGRES_PASSWORD=odoo -e POSTGRES_DB=postgres `
  -v odoo-db-data:/var/lib/postgresql/data `
  postgres:15

podman run -d --name odoo --network odoo-net -p 8069:8069 `
  -e HOST=odoo-db -e USER=odoo -e PASSWORD=odoo `
  -v odoo-web-data:/var/lib/odoo `
  odoo:17
```

## 3. Create the database (browser)

1. Go to `http://localhost:8069` once both containers show as running.
2. Fill in:
   - **Database name:** `huma` (or anything — you'll need this exact name for the agent's config).
   - **Email / Password:** this becomes your admin login.
   - **Demo data:** leave **unchecked**.
3. Submit and wait.

## 4. Install the Invoicing app

Apps (left sidebar) → search "Invoicing" → Install. This is the free app; it's all `account.move` needs.

## 5. Auth for the agent

Simplest path (fine for a local throwaway instance): the agent uses your admin **email + password** directly over XML-RPC.
Put these in `.env` (template provided in the repo):

```
ODOO_URL=http://localhost:8069
ODOO_DB=huma
ODOO_USERNAME=<the admin email you set>
ODOO_PASSWORD=<password or API key>
```

## 6. Smoke test

```powershell
python -c "
import xmlrpc.client
url, db, user, pw = 'http://localhost:8069', 'huma', '<email>', '<password>'
common = xmlrpc.client.ServerProxy(f'{url}/xmlrpc/2/common')
uid = common.authenticate(db, user, pw, {})
print('Authenticated as uid:', uid)
"
```

If that prints a UID (not `False`/an error), the agent's Odoo client will work as-is.
