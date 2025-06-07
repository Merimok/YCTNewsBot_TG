# AutoNews

AutoNews is a Telegram bot that automatically collects news from RSS feeds, generates concise summaries in Russian using AI (OpenAI), and posts them to a specified Telegram channel. The bot supports configuration management, administration, and monitoring through Telegram commands.

## Key Features

- **Automated News Parsing**: Fetches news from popular RSS feeds (The Verge, TechCrunch, Ars Technica, etc.).
- **AI-Generated Content**: Uses OpenAI (e.g., `gpt-4o-mini` model) to create headlines and brief news summaries in Russian.
- **Telegram Posting**: Automatically posts news to a designated channel at set intervals.
- **Configuration Management**: Allows customization of posting intervals, AI model selection, prompt editing, and admin management.
- **Caching and Filtering**: Uses SQLite to store a news cache and prevent duplicates.
- **Monitoring and Logging**: Tracks post counts, errors, and duplicates, with optional error notifications.
- **Database Backup**: Supports exporting and importing the SQLite database via Telegram.

## Requirements

- Python 3.10+
- Dependencies:
  ```bash
  pip install flask feedparser requests openai
  ```
- Environment Variables:
  - `TELEGRAM_TOKEN`: Your Telegram bot token.
  - `OPENAI_API_KEY`: Your OpenAI API key.
- A Telegram channel where the bot has admin privileges.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/goreforced/YCTNewsBot_TG.git
   cd YCTNewsBot_TG
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Set environment variables:
   ```bash
   export TELEGRAM_TOKEN="your-telegram-bot-token"
   export OPENAI_API_KEY="your-openai-api-key"
   ```

4. Run the application:
   ```bash
   python bot.py
   ```

5. Set up a Telegram webhook:
   - Ensure your server is accessible via HTTPS.
   - Send the request:
     ```bash
     curl -F "url=https://your-server.com/webhook" https://api.telegram.org/bot<your-telegram-token>/setWebhook
     ```

## Usage

1. Add the bot to a Telegram channel and grant it admin privileges.
2. Interact with the bot using Telegram commands:

   **Core Commands**:
   - `/start` — Bind a channel or check access.
   - `/startposting` — Start automatic posting.
   - `/stopposting` — Stop posting.
   - `/setinterval <time>` — Set posting interval (e.g., `34m`, `1h`, `2h 53m`).

   **Configuration**:
   - `/editprompt` — Edit the AI prompt.
   - `/changellm <model>` — Switch AI model (e.g., `gpt-4o-mini`).
   - `/errnotification <on/off>` — Enable/disable error notifications.

   **Monitoring**:
   - `/info` — Show bot status.
   - `/errinf` — Display recent errors.
   - `/feedcache` — Show news cache.
   - `/feedcacheclear` — Clear the cache.

   **Administration**:
   - `/addadmin <@username>` — Add an admin.
   - `/removeadmin <@username>` — Remove an admin.
   - `/sqlitebackup` — Export the SQLite database.
   - `/sqliteupdate` — Import the SQLite database.

   **Additional**:
   - `/nextpost` — Reset the timer and post immediately.
   - `/skiprss` — Skip the next RSS feed.
   - `/help` — Show the command list.

## Database Structure

The bot uses SQLite (`feedcache.db`) to store data. Main tables:

- `feedcache`: News cache (ID, title, summary, link, source, timestamp).
- `channels`: Channel information (channel ID, creator).
- `admins`: List of channel admins.
- `config`: Settings (AI prompt, model, error notifications).
- `errors`: Error log (timestamp, message, link).

## Logging

- Logs are output to the console in the format: `%(asctime)s - %(levelname)s - %(message)s`.
- Logging level: `INFO`.

## Limitations

- The bot only works with public RSS feeds.
- A valid OpenAI API key is required for content generation.
- Telegram limits message length to 4096 characters, so long summaries are truncated.
- The bot must be an admin in the target channel.

## License

GPL-3.0 License. See the `LICENSE` file for details.

## Contact

For questions or suggestions, create an issue in the repository.
