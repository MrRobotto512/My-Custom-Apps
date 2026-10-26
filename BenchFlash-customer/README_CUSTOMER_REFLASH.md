# Rebuilding your BenchTool card — customer guide

Your BenchTool bench runs from a microSD card. SD cards wear out and, now
and then, corrupt. When that happens the bench itself is fine — it just
needs a fresh card, and this guide gets you one in about an hour, most of
which is waiting. You do not need to know anything about Raspberry Pis.

You will need:

- a **Windows PC (10 or 11) or a Mac**,
- the **BenchFlash** app you were sent (`BenchFlash.exe` or `BenchFlash.app`),
- a **blank microSD card, 32 GB or larger**, and a card reader,
- your **Wi-Fi name and password** (unless the bench uses an Ethernet cable),
- about **20 minutes of attention** and then **30 minutes of leaving it alone**,
- **if you want your repairs, letterhead, printer and keys back:** your
  backup file and its passphrase (see *Restoring your data* below).

---

## Is it really the card?

A corrupted card looks like one of these. Any of them means: rebuild the card.

| What you see | What it means |
|---|---|
| **A rainbow / four-colour square** and nothing else, or the screen stays on the Raspberry Pi splash | the Pi's firmware cannot read the card at all |
| **Boot loop** — the screen comes up part way, goes black, comes up again, over and over | the system on the card is damaged and keeps crashing on start |
| Text on screen ending in **`VFS: Cannot open root device`**, **`Kernel panic — not syncing`** or **`mmc0: error -110`** | the card's main partition is unreadable |
| Dropped to a prompt that says **`(initramfs)`** or **`emergency mode`** / **`Give root password for maintenance`** | the filesystem failed its check and could not be repaired |
| The bench **worked until a power cut** and now shows one of the above | a write was interrupted — the classic SD corruption |
| **Red LED on, green LED never blinks** when powered | nothing bootable was found on the card (or no card) |

If instead the bench boots to BenchTool but a *tool* misbehaves, the card is
fine — use Settings ▸ Software & Updates ▸ Firmware Update, and contact the
seller if that does not help.

Before you start: **pull the card out and put it back in** once, firmly.
A card that is not fully seated produces the same rainbow screen as a dead
one. If it then boots, back it up straight away (Settings ▸ Software &
Updates ▸ Backup) — a card that has started doing this will do it again.

---

## Step by step

### 1. Open BenchFlash

- **Windows:** double-click `BenchFlash.exe`. Windows asks *"Do you want to
  allow this app to make changes?"* — answer **Yes** (writing a card needs
  administrator rights). Windows SmartScreen may first say *"Windows protected
  your PC"* because the app is not signed: click **More info → Run anyway**.
- **macOS:** the app is not signed with an Apple certificate, so the first
  launch is refused by Gatekeeper (*"cannot be opened because the developer
  cannot be verified"*). **Right-click (or Control-click) `BenchFlash.app` →
  Open → Open.** After that it opens normally. Writing a card needs root, so
  if the app says it is not elevated, open Terminal and run:

  ```bash
  sudo /Applications/BenchFlash.app/Contents/MacOS/BenchFlash
  ```

  (adjust the path to wherever you put the app). It asks for your Mac
  password once.

### 2. Put the blank card in the reader

Then press **Rescan** in box 1 until the card is listed. Only removable
cards are ever listed — your PC's own drive cannot be picked.

### 3. Board and screen (box 2)

Leave these as they are unless you know the bench is different. Most
benches are a **Raspberry Pi 4** with the **Hosyond 7" touchscreen**. If you
restore a backup in step 5, these are filled in from the backup.

### 4. This bench (box 3) and network (box 4)

- **Hostname** is the bench's name, e.g. `benchtool-03`. It is printed on
  the sticker on the bench, and it is in the name of every backup file
  (`benchtool-03-20261021-030000.tar`). If you restore a backup it is filled
  in for you — leave it alone.
- **Username / password** — the bench's login. Use what you had, or make a
  new one; it only matters if you ever plug in a keyboard.
- **Wi-Fi name, password, country.** Leave the Wi-Fi name blank if the bench
  is on an Ethernet cable.

### 5. Restore from backup (box 6, optional but recommended)

If the bench had backups turned on, every night it put a file named
`<hostname>-<date>.tar` in a folder called **BenchTool Backups** on your
Google Drive, OneDrive, USB stick or Mac. Download the newest one to this
PC, press **Choose backup file…**, pick it, and type the **backup
passphrase** you set on the bench. BenchFlash checks the file *before* it
touches the card; a wrong passphrase just asks again. When it says ✓, the
hostname and screen are filled in from the backup.

The restore brings back: every repair and photo, the receipt letterhead and
logo, the printer, the AI keys, the backup settings, and the licence. It
runs on the bench after the install, by itself.

No backup? Skip this box. The bench comes up empty but working, like the
day it was delivered. (And turn backups on this time — Settings ▸ Software
& Updates ▸ Backup.)

### 6. The key (box 5) — read this one carefully

Press **Prepare key**. The box fills with a long line of text starting with
`ssh-ed25519`. This is a key that lets *this bench, and only this bench*
download its software. It has to be approved by the seller before it works.

1. Press **Copy key**.
2. **Paste it into a message to the seller** — the contact is printed right
   there in the box — and send it. Email, text, whatever is easiest; the key
   is public and safe to send.
3. **Wait.** The screen says *"Waiting for … to add the key"* and checks by
   itself every few seconds. When the seller has added it, the line turns
   green and the big button at the bottom changes to **FLASH THIS CARD**.
   This can take a minute or a day depending on when the seller sees your
   message. You can close BenchFlash and come back: *Prepare key* finds the
   same key again.

**Do NOT put the card in the bench before the button has unlocked.** The
button is disabled on purpose, not just a warning: a card flashed with a
key the seller has not added yet does not fail with a message — the bench
sits forever looking busy and never finishes installing. BenchFlash will
not let you write such a card.

### 7. Flash

Press **FLASH THIS CARD**, confirm. BenchFlash downloads the operating
system (about 1 GB, cached for next time), writes the card, and sets it up.
Expect **5–15 minutes**. When it says *Card ready*, the card has been
ejected — pull it out.

### 8. Boot the bench — and leave it alone

Put the card in the bench, plug in power (and the Ethernet cable, if it uses
one). Then **do nothing for 30 minutes**. The bench:

1. boots and sets itself up (1–2 minutes, then reboots),
2. downloads and installs BenchTool (15–25 minutes; a black screen with
   text, or the splash screen, is normal — the touchscreen may stay dark
   until the end on EDATEC benches),
3. reboots into BenchTool,
4. **if you chose a backup:** puts your data back and reboots once more.

When the BenchTool home screen is up, you are done. If you restored a
backup, open Settings ▸ Software & Updates ▸ Backup and check that it says
*last backup* with a date and that the destination is still signed in; sign
in again if it is not.

---

## If something goes wrong

| Symptom | What to do |
|---|---|
| BenchFlash says **git / ssh was not found** | Windows: Settings → Apps → Optional features → add **OpenSSH Client**, then restart BenchFlash. macOS: this does not happen. |
| The key line stays amber for a long time | The seller has not added it yet. Check you sent the whole line (it is long). Press *Prepare key* again to make sure the same key is shown. |
| **"That passphrase does not open this backup"** | It is the passphrase set on the *bench's* Backup screen, not your Google/Microsoft password. Try another backup file if you changed it at some point. |
| **"The backup is … MB but the card's boot partition only has … MB free"** | The backup is too big to ride on the card. Flash without it, then on the bench use Settings ▸ Software & Updates ▸ Backup ▸ **RESTORE** with the file on a USB stick. |
| The bench has been on for an hour and shows a desktop or a text screen | Take the card out, put it in the PC, and open the file `benchtool-install-status.txt` on it. It says in one sentence what is wrong — usually the Wi-Fi password, or the key. Send that file to the seller. |
| The bench shows a **licence / activation** screen after a restore | The backup came from a different bench (or the bench's board was replaced). Send the Device ID on that screen to the seller for an unlock code. |

---

## For the seller (Ryan)

- **Build:** edit `benchflash/customer.json` (your name, email, phone) and
  from the repo root run `pyinstaller benchflash/BenchFlash.spec` on
  Windows for `dist/BenchFlash.exe` and on a Mac for `dist/BenchFlash.app`.
  The built app is always in customer mode; `py BenchFlash.pyw` from the
  checkout stays the operator build. Details in `benchflash/README.md`.
- **When a key arrives:** GitHub → the repo → Settings → Deploy keys → Add
  deploy key, title it with the bench's hostname, paste, *Allow write
  access* OFF. The customer's screen unlocks within seconds. Delete the
  bench's *old* key (same hostname) while you are there — the old card is
  dead and its key with it.
- **Both builds are unsigned.** Signing/notarizing is the only way to remove
  the SmartScreen and Gatekeeper prompts above; see "Known gaps" in
  `benchflash/README.md`.
