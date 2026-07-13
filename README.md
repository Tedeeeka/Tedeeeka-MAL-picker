# MAL Picker Helper

Adding anime to MyAnimeList one at a time — click, wait for the popup, set the status, search for the next one — is painfully slow if you've got a backlog of hundreds of shows. This tool fixes that: browse anime visually by season or search, tag a status with one click (or a keyboard shortcut), and export a file MAL will import in one shot.

Available in English and Latin American Spanish, switchable with one click in the app.

## What it does

- Browse anime by year + season, or search by title
- Tag each one as Watching / Completed / On-Hold / Dropped / Plan to Watch (or leave it untagged)
- Optional 0–10 scoring
- Hover a card and press `1`–`6` to tag it without touching your mouse
- Saves your progress automatically to a plain file next to the program
- Export a MyAnimeList-compatible XML file, ready to upload at `myanimelist.net/import.php`
- English and Latin American Spanish, switchable with one click
- Light/dark theme, and yes, you can put a custom banner image up top

## Getting it

Grab the right file for your OS from the [Releases](../../releases) page (or the latest [Actions run](../../actions) if there's no formal release yet):

- **Windows:** `mal_picker_windows.exe`
- **macOS:** `mal_picker_macos`
- **Linux:** `mal_picker_linux`
- **Android:** coming soon

Double-click it. A console window opens and your browser should launch automatically to the tool. Close the console window when you're done to stop it.

**Windows will likely show a "Windows protected your PC" SmartScreen warning the first time.** That's expected — see below for why, and how to check for yourself that it's safe.

## Why you can trust this

I get it, some random exe from a friend is exactly the kind of thing you shouldn't just run blindly — so here's why this one's fine:

- **It's one plain Python file.** `app.py` is the whole program, nothing else. No compiled mystery blob, nothing hidden — anyone can open it and read exactly what it does line by line.
- **It only talks to two places, period:** [AniList](https://anilist.co) to pull anime titles/covers/dates, and MyAnimeList — and only that one, only when *you* hit export and upload the file yourself. No hidden server, no analytics, no phoning home.
- **Your tags never leave your computer.** They're saved in `mal_picker_progress.json` right next to the program. Open it in Notepad if you want, it's just plain readable text.
- **About that Windows warning** — it pops up because the exe isn't digitally signed, and getting a signing certificate costs actual money for a free hobby tool, that's it. It's not flagging anything malicious, just "I don't recognize this publisher." If you'd still rather not trust a prebuilt exe, skip straight to "Build it yourself" below and compile your own from the source sitting right here in this repo.

## Build it yourself

If you don't want to run a prebuilt executable, it's two commands (Python 3 required):

```
pip install pyinstaller
pyinstaller --onefile app.py
```

That produces your own binary from the source, straight from this repo.

Or skip building entirely and just run it directly:

```
python3 app.py
```

## How this was made

This entire tool — the app, the UI, the packaging, this README — was built by Claude (Anthropic's AI). The idea, the requirements, and the testing were mine; the code was AI-generated from start to finish.

## License

MIT — see [LICENSE](LICENSE). Use it, modify it, share it, whatever.
