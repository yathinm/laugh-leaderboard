# iMessage Laugh Board

Rank **Haha, Heart, Thumbs Up, and Thumbs Down** reactions in an iMessage
chat — group or individual — on your Mac only.

Nothing is uploaded. Your messages never leave your computer.

## What you get

After one Terminal command, a local webpage shows:

- **Reaction rankings** (received / given / per message / per 1k characters)
- **Who → Whom** matrices for Haha, Heart, Thumbs Up, and Thumbs Down
- **Bar race** over time
- **When hours** heatmap (Eastern Time)
- **Daily activity** (busiest day, average texts per day, weekday breakdown)
- **Funniest** messages (Hall of Fame + setup thread)
- **Clips** (TikTok / IG Reels / YouTube Shorts)

## Requirements

- A **Mac** with the chat history in the **Messages** app
- **Full Disk Access** for Terminal (one-time permission)

## Steps (non-coders)

### 1. Allow Terminal to read Messages

1. Open **System Settings → Privacy & Security → Full Disk Access**
2. Turn **on** for **Terminal** (or iTerm)
3. Quit Terminal completely, then open it again

### 2. Download this project

In Terminal:

```bash
cd ~/Desktop
git clone https://github.com/yathinm/laugh-leaderboard.git
cd laugh-leaderboard
```

If macOS asks to install developer tools, click **Install**, wait, then run the `git clone` line again.

### 3. Export your chat

```bash
./get-laughs.sh
```

Answer the prompts (group name, whether to include 1-on-1 chats, and auto-contacts).  
That creates:

- `~/Desktop/laugh-data.json` ← use this in the UI  
- `~/Desktop/laugh-leaderboard.csv` ← simple spreadsheet  
- `laugh-data.json` in this folder too

### 4. Open the board

```bash
open index.html
```

Or double-click **index.html** in Finder.

Drop **laugh-data.json** onto the page.

If any contacts are still just phone numbers or emails, you'll be prompted to
name them. Contacts that already have names (from `--auto-contacts`) skip this step.

> Tip: to try the UI with fake data first, drop **sample-data.json**, or run `python3 -m http.server 8765` in this folder and open `http://localhost:8765` (the Sample button works that way).

## Privacy

- Reads only your local `~/Library/Messages/chat.db` copy
- The webpage loads the JSON in your browser with no network upload
- Do not commit `laugh-data.json` if it has real chats

## Optional: friendly names

Phones/emails are shortened automatically. For custom names, create `names.json`:

```json
{
  "me": "Yathin",
  "+15551234567": "Friend",
  "friend@icloud.com": "Friend"
}
```

Then:

```bash
python3 export_laughs.py --names names.json
```

Or use `--auto-contacts` to pull names straight from your Contacts app (no manual mapping needed):

```bash
python3 export_laughs.py --auto-contacts
```

## For people who like flags

```bash
python3 export_laughs.py --list
python3 export_laughs.py --chat "group name" --all-matching
python3 export_laughs.py --chat-id 42
python3 export_laughs.py --include-individual
python3 export_laughs.py --list --include-individual
python3 export_laughs.py --auto-contacts
python3 export_laughs.py --chat "Hailey" --include-individual --auto-contacts
```

## Notes

- Tapback types: Heart `2000`, Thumbs Up `2001`, Thumbs Down `2002`, Haha `2003`
- Only use this on chats where everyone is comfortable with the analysis
