# Self Manager — Film Menu Edition

This build includes the existing self-manager menu, user/diamond balance system, and a group mini-game feature.

## Group mini-games
- In a group, send `بازی 20` (or another integer from 1 to 1000) to create a challenge. The number is the **score reward**, not a diamond stake.
- Choose two or three players, then choose bot or other users.
- User-vs-user games start automatically when the lobby fills.
- Winner selection uses RANDOM.ORG's public integer endpoint; if the endpoint is unavailable, a cryptographically secure local fallback is used and the result identifies that fallback.
- Scores are separate from diamonds. Games never deduct, transfer, or multiply diamonds. Use `/gamepoints` to view your score.
- The bot needs to be able to read group messages to detect `بازی 20`; adjust Telegram BotFather privacy settings if needed.

## Deployment
Keep the existing environment variables (`BOT_TOKEN`, `OWNER_ID`, `DATABASE_URL`, `TG_API_ID`, `TG_API_HASH`, `SESSION_SECRET`). Set `SESSION_SECRET` to a private random string of at least 32 characters (no quotes). This build accepts both a valid Fernet key and a long passphrase; passphrases are converted to a Fernet key automatically. Keep the same value across redeploys or previously encrypted sessions may not decrypt. Existing database tables are preserved; the game tables are created automatically at startup.

## Limitations
A source-code syntax check can be performed locally, but a complete live integration test requires valid Telegram credentials, a database, and a test group. RANDOM.ORG is an external service and may rate-limit or be temporarily unavailable.
