# SELF MMD Telegram Panel

## Railway deployment
1. Upload the **contents** of this project to the root of the GitHub repository (do not upload the ZIP itself).
2. Ensure `bot.py`, `Dockerfile`, `requirements.txt`, and `assets/mmd_self_menu.png` are at the shown paths.
3. Deploy the repository on Railway and configure the required environment variables in Railway Variables.

## Menu
The SELF MMD feature menu is a single page (no 7-page navigation). The artwork is stored at `assets/mmd_self_menu.png` and is sent above the panel keyboard. Manager-only controls are added only for the owner/delegated managers.

## Secrets
Never commit `BOT_TOKEN`, `TG_API_HASH`, `SESSION_SECRET`, login codes, passwords, or Telegram session strings to GitHub. Store secrets in Railway Variables.
