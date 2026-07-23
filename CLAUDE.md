# Collis Family Planner — Claude Instructions

## Deployment

The app runs on an OCI server at `100.111.136.33` (SSH alias `cfp` in `~/.ssh/config`). SSH access requires Tailscale VPN. This machine is Ubuntu-only — the old Windows dev machine was retired during the 2026-07 migration; do not reference PowerShell/`.ps1` paths.

### Two Tailscale accounts in use on this machine

- `pudsoft@gmail.com` — Rythm OCI (wrong one for this project)
- `tcnskynet@gmail.com` — CCFP OCI at `100.111.136.33` (correct one)

### SSH helper script

Use `~/projects/claude-helpers/claude-helpers/cfp_ssh.sh` for all connections.
Always pass `--force` so it auto-switches Tailscale without prompting.

```bash
# Deploy (git pull + restart service)
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh deploy --force

# Run a single remote command
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh run "sudo journalctl -u collis-family-planner -n 50" --force

# Check service status
~/projects/claude-helpers/claude-helpers/cfp_ssh.sh status --force
```

The `--force` flag auto-switches Tailscale to `tcnskynet@gmail.com`, deploys, then restores the previous account. Never attempt raw SSH manually as a workaround. Always commit + push before deploying — the server only does a `git pull`.

### SSH key

The script looks for the key at `~/.ssh/CCFP_HelpersOraclecfp-prod1.key`, already in place on this machine.
