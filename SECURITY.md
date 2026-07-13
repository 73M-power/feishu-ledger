# Security Policy

## Secrets

Do not commit any of the following values:

- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_WIKI_TOKEN`
- `FEISHU_BITABLE_APP_TOKEN`
- `FEISHU_WEBHOOK`
- `CF_TUNNEL_TOKEN`

Use Render Environment Variables or GitHub Actions Secrets for production configuration. The `.env.example` file contains placeholders only.

## If a secret was committed

1. Revoke or regenerate the value in Feishu, Render, Cloudflare, or GitHub immediately.
2. Remove the value from the current working tree.
3. Remove it from Git history before treating the repository as clean.
4. Force-push the rewritten history only after confirming that all collaborators understand the impact.

Removing a secret from the latest file does not remove it from old Git commits. Anyone who can read the repository history may still retrieve the old value.

## Reporting

For a private report, contact the repository owner through GitHub instead of opening a public issue containing the secret.
