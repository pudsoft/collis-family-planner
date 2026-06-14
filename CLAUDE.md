# Collis Family Planner — Claude Instructions

## Deployment

The app runs on an OCI server at `100.111.136.33`. SSH access requires Tailscale VPN.

### Two Tailscale accounts in use on this machine

- `pudsoft@` — Rythm OCI (wrong one for this project)
- `tcnskynet@` — CCFP OCI at `100.111.136.33` (correct one)

### SSH helper script

Use `C:\Claude_Helpers\claude-helpers\cfp_ssh.ps1` for all connections.
Always pass `-Force` so it auto-switches Tailscale without prompting.

```powershell
# Deploy (git pull + restart service)
& "C:\Claude_Helpers\claude-helpers\cfp_ssh.ps1" deploy -Force

# Run a single remote command
& "C:\Claude_Helpers\claude-helpers\cfp_ssh.ps1" run "sudo journalctl -u collis-family-planner -n 50" -Force

# Check service status
& "C:\Claude_Helpers\claude-helpers\cfp_ssh.ps1" status -Force
```

The `-Force` flag auto-switches Tailscale to `tcnskynet@gmail.com`, deploys, then restores the previous account. Never attempt raw SSH manually as a workaround.

### SSH key

The script looks for the key at `$env:USERPROFILE\.ssh\CCFP_HelpersOraclecfp-prod1.key` (i.e. the current Windows user's `.ssh` folder). The authorised key on the server lives under `C:\Users\TCN\.ssh\` — if running as a different Windows user, copy it once:

```powershell
Copy-Item "C:\Users\TCN\.ssh\CCFP_HelpersOraclecfp-prod1.key" "$env:USERPROFILE\.ssh\CCFP_HelpersOraclecfp-prod1.key" -Force
```
