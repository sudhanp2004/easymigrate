# Deploying the hosted EasyMigrate (easymigrate.sudhan.tech)

The hosted version is the read-only free report: visitors connect AWS through a role they
create, scan, review and download the GCP Terraform. Design:
`docs/superpowers/specs/2026-10-28-hosted-report-design.md`.

```
easymigrate.sudhan.tech      Vercel   frontend/ built with `npm run build:hosted`
api.easymigrate.sudhan.tech  Render   render.yaml (FastAPI: easymigrate.web)
database                     Neon     Postgres; workspaces + encrypted project archives
role template                S3       deploy/aws/easymigrate-readonly-role.json
```

## One-time setup

1. **Database (Neon).** Create a project and copy its pooled connection string
   (`postgresql://...?sslmode=require`). Tables are created on first start.
2. **Operator AWS identity** (account 538342414628). IAM user `easymigrate-web` with one
   inline policy and an access key:
   ```json
   {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
     "Resource": "arn:aws:iam::*:role/EasyMigrateReadOnly-*"}]}
   ```
   The visitor template trusts exactly `arn:aws:iam::538342414628:user/easymigrate-web`. If the
   user is ever deleted and recreated, existing visitor roles stop trusting it (IAM pins the
   user's internal ID); visitors then recreate their stack.
3. **Role template.** `deploy/aws/publish-template.sh <bucket>` prints the template URL.
   Re-run it whenever the template or `docs/aws-scan-policy.json` changes.
4. **Data key.** `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
   Changing it makes every stored workspace unreadable (visitors start again).
5. **Render.** New → Blueprint → this repository. Fill the `sync: false` variables:
   `DATABASE_URL`, `EASYMIGRATE_DATA_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
   `EASYMIGRATE_CFN_TEMPLATE_URL`. Then Settings → Custom Domains → `api.easymigrate.sudhan.tech`.
6. **Vercel.** Add New → Project → this repository, Root Directory `frontend`, framework
   Vite (build settings come from `frontend/vercel.json`). Then Settings → Domains →
   `easymigrate.sudhan.tech`.
7. **DNS** for `sudhan.tech`: `easymigrate` CNAME → the target Vercel shows;
   `api.easymigrate` CNAME → the target Render shows. Both issue HTTPS certificates.

Pushing to `main` redeploys both.

## Operating it

- Free tiers: the API sleeps after 15 minutes idle and takes up to a minute to wake (the site
  says so). Render Starter (~$7/month) keeps it awake; no code change.
- Workspaces are deleted 7 days after their last activity (hourly cleanup).
- Limits: 10 new sessions per IP per hour, 10 scans per session per day, 2 scans at once.
- Local run: `EASYMIGRATE_WEB_DEV=1 uv run --extra web uvicorn easymigrate.web.app:create_web_app --factory`
  plus `npm --prefix frontend run dev -- --mode hosted` with `VITE_API_BASE=http://127.0.0.1:8000`.
