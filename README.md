# Remote Mic: a Teams/Meet mute remote for your phone

There are two ways to use it. Neither needs you to run `server.py` by hand.

| | **A. Bluetooth app** (`RemoteMic.apk`) | **B. Wi-Fi agent** (`RemoteMic.exe`) |
|---|---|---|
| Laptop software | **None.** The phone pairs as a Bluetooth keyboard | One .exe, no Python, starts with Windows |
| Teams Ctrl+Shift+M / Meet Ctrl+D | ✅ to the window in front | ✅ finds and focuses the right window |
| Windows 11 call mute (Win+Alt+K) | ✅ works from any window | (use the Teams key) |
| Push to talk | ✅ hold Ctrl+Space, or Win+Alt+K in and out | ✅ hardware mic or Teams |
| Mute the mic hardware / one app (e.g. only RDP) | ❌ a keyboard can't do this | ✅ |
| Works when office Wi-Fi blocks phones | ✅ | ❌ (use your hotspot) |

## How the three pieces fit together
- **RemoteMic.exe** runs on the laptop and serves the remote page (the web app) on your Wi-Fi. Its pairing page shows a QR code.
- **Phone browser:** scan the QR code to get the web app with full control. On Android, tap **open in the Remote Mic app** to hand the link to the app.
- **RemoteMic.apk** has two modes with the same keys and flow. **Bluetooth** needs no laptop software. **Wi-Fi** opens the same web app from RemoteMic.exe inside the app. Switch with **⇄ switch to bluetooth mode** at the bottom of the Wi-Fi page, or under **wi-fi agent** on the Bluetooth page, where you can also paste the agent link. If the laptop can't be reached, the app falls back to Bluetooth and says so.
- **Hosted page** (Vercel): a demo of the web app with download links. A browser can't reach your laptop from an https site, so the real controls always come from RemoteMic.exe.

## Get the builds (one time, about 5 minutes)
1. Create an empty GitHub repo (e.g. `remote-mic`) and push this folder to it.
2. Open the repo's **Actions** tab. The `build` workflow makes both `RemoteMic.apk` and `RemoteMic.exe`.
3. To install the APK straight from your phone, push a tag (`git tag v1.0 && git push --tags`). The files then appear on the repo's **Releases** page.

Or open the `android/` folder in Android Studio and press Run.

## A. Bluetooth app setup
1. Install `RemoteMic.apk` (Android 9+). Allow "install unknown apps" for your browser or Files app.
2. Open it and allow the Bluetooth permissions. Tap **pair a new laptop**.
3. On the laptop: **Settings › Bluetooth & devices › Add device › Bluetooth** → pick your phone → confirm the code.
4. Done. Windows sees a keyboard called "Remote Mic", and the app reconnects to it automatically from then on.

Keys: **01 teams** = Ctrl+Shift+M · **03 call mute** = Win+Alt+K · **04 meet** = Ctrl+D · **02/05** switch between paired laptops · **06** push-to-talk mode. All shortcuts can be changed under *key map*.

Notes:
- Ctrl+Shift+M and Ctrl+D go to the window **in front**. For an RDP call, keep the Remote Desktop window in front (with Teams in front inside it).
- Win+Alt+K is Windows 11's own call mute. It works from any window for Teams and the taskbar mic icon shows the change. It needs Windows 11 and a call app that supports it (Teams does).
- Keep the app open during a call (the screen stays on). Leaving the app releases a held push-to-talk key.
- A few phones (some older Samsung / Xiaomi firmware) block "phone as keyboard". The app says so if yours does.
- Company laptops sometimes block new Bluetooth devices. If pairing is refused, use the Wi-Fi agent.

## B. Wi-Fi agent setup
1. Run `RemoteMic.exe`. A pairing page with a QR code opens in your browser.
2. Tick **Start with Windows** once. It then runs quietly at every login. Run the .exe again any time to see the QR code.
3. Scan the QR code with your phone (same Wi-Fi or hotspot) → **Add to Home screen**.
4. Allow Python/RemoteMic on **Private networks** if the Windows Firewall asks.

## Wi-Fi agent: the controls
| Control | What it does | Local Teams/Meet | RDP (remote audio) |
|---|---|---|---|
| **Big button** | Mutes every microphone on the laptop through Windows (built-in, headset, USB). Re-applied every second, so a mic you plug in mid-call is muted too. | ✅ | ✅ The RDP client only forwards what your mic picks up |
| **Teams (Ctrl+Shift+M)** | Brings the Teams meeting window to the front, sends the shortcut, then switches back to your previous window | ✅ | ✅ Sends the shortcut to the mstsc/msrdc window, and RDP passes it to the remote Teams |
| **Meet (Ctrl+D)** | Same, for the browser tab with Google Meet | ✅ | ✅ (pick RDP) |
| **Push to talk: Mic mode** | Keep the big button on MUTED. Holding the pad turns every mic on; letting go mutes again | ✅ | ✅ |
| **Push to talk: Teams mode** | Stay muted inside Teams. Holding the pad holds Ctrl+Space, Teams' own "temporarily unmute" shortcut. The Auto / This laptop / RDP switch picks the window | ✅ | ✅ |
| **🔒 Lock** | Keeps talking without holding the pad. Tap the pad or the button again to stop | ✅ | ✅ |
| **Mute one app's mic** | Mutes just one app's microphone stream. `mstsc.exe` / `msrdc.exe` = only the RDP session; `ms-teams.exe` = only local Teams. Stays muted until you turn it off. | ✅ | ✅ |
| **Microphones** | Mute or unmute each mic on its own | ✅ | ✅ |
| **Detected call windows** | Tap a window to always send the shortcuts there | | |

**Target switch:** *Auto* sends to whichever Teams or RDP window you used most recently. *This laptop* and *RDP* force one or the other.

## Push-to-talk safety
While you're talking, the phone checks in with the laptop every 0.7 s. If those check-ins stop for 2.5 s (screen locked, app closed, Wi-Fi dropped), the laptop mutes you again on its own, so you never get stuck with an open mic. Change the 2.5 s with `ptt_timeout_sec` in `config.json`. Leaving or hiding the page also releases straight away.

In Teams mode, the Teams window stays in front while you hold, then your previous window comes back. Don't type on the laptop while holding, because Ctrl is held down.

## Notes and limits
- **For RDP + Teams shortcut:** inside the remote session, Teams should be the active window (or the meeting window should be open on top). The shortcut goes to whatever is focused inside the RDP session.
- The Teams/Meet buttons are **toggles**. Windows can't tell the agent whether Teams itself shows you as muted, so check Teams after tapping. The big button and the app/mic switches show their real state.
- **Safest mute:** use the big button or the per-app switch. They cut the sound at the Windows level, so nothing reaches Teams even if the shortcut misses.
- The shortcuts briefly bring the call window to the front (~0.2 s), then switch back.
- Per-app mute uses the Windows audio settings for each app's mic stream. It works with Teams and RDP on standard Windows 10/11. Test it once before relying on it in an important call.
- Settings live in `config.json`, which is created on first run. There you can change the port, the token (delete it to make a new one), process names (for example, add `wfica32.exe` for Citrix), the shortcuts, and `mute_on_start`.
- Try the UI without Windows: `python server.py --mock`.

## Security
API calls require the random token in the link. Anyone on the same network who has the link can control your mic, so don't share it. To revoke access, delete `token` from `config.json` and restart.
