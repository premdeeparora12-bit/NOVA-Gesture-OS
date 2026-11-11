# NOVA Gesture OS

Control your Windows PC with hand gestures through your webcam. NOVA uses Google MediaPipe hand tracking and shows a small always-on-top overlay that keeps working while you use any other app.

## Features

- Volume up / down with a thumbs up / thumbs down (keeps repeating while you hold it)
- Mute / unmute with a peace sign
- Screenshot to your Desktop with a pinch (with a white flash and sound)
- Open YouTube with both thumbs up
- Draggable overlay that remembers its position
- Tray icon, global hotkeys, and a JSON settings file
- Camera picker, automatic reconnect if the camera drops
- Works with both the old and the new MediaPipe (so it runs on Python 3.14 too)

## Gestures

| Gesture | Hold time | Action |
|---|---|---|
| Thumbs up | 0.3 s | Volume up (repeats while held) |
| Thumbs down | 0.3 s | Volume down (repeats while held) |
| Peace sign (V) | 0.5 s | Mute / unmute |
| Pinch (thumb + index) | 0.25 s | Screenshot saved to Desktop |
| Both thumbs up | 0.5 s | Open YouTube |

A progress ring appears over your hand while a gesture is being held. When it fills, the action runs.

## Hotkeys (work from any app)

| Keys | Action |
|---|---|
| Ctrl + Alt + Q | Quit |
| Ctrl + Alt + P | Pause / resume |
| Ctrl + Alt + H | Hide / show the overlay |

You can also drag the overlay to move it, click `II` to pause, and click `X` to quit.

## Requirements

- Windows 10 or 11
- Python 3.10 or newer (3.11 / 3.12 recommended)
- A webcam

## Installation

1. Install Python from python.org. Tick "Add python.exe to PATH" during setup.
2. Open PowerShell in the project folder and run:

```
python -m pip install -r requirements.txt
```

3. Start NOVA:

```
python nova.py
```

On the first run it asks you to pick a camera (if you have more than one) and remembers your choice. With the newer MediaPipe it also downloads a small hand model (about 8 MB) once and saves it as `hand_landmarker.task`.

## Command line options

```
python nova.py                  normal start
python nova.py --pick-camera    choose the camera again
python nova.py --camera 1       force a camera index
python nova.py --debug          show finger states and the raw gesture
python nova.py --no-tray        do not create a tray icon
python nova.py --autostart on   start with Windows  (use "off" to remove)
```

## Settings

NOVA creates `nova_config.json` next to `nova.py` on first run. Edit it while NOVA is closed.

| Setting | Meaning |
|---|---|
| `volume_step` | Percent changed per step (default 5) |
| `repeat_ms` | Delay between repeats while holding thumbs up / down |
| `hold_ms` | How long each gesture must be held |
| `cooldown_ms` | Minimum time before a gesture can fire again |
| `actions` | What each gesture does: `volume_up`, `volume_down`, `mute`, `screenshot`, `youtube`, or `none` |
| `youtube_url` | The page opened by the YouTube gesture |
| `browser` | `firefox` or anything else for the default browser |
| `screenshot_dir` | Where screenshots go (empty = Desktop) |
| `flash`, `sound` | Turn the screenshot flash and sound on or off |
| `min_detection_confidence`, `min_tracking_confidence` | Hand tracking sensitivity |

Example: make the peace sign take screenshots instead:

```json
"actions": { "peace": "screenshot", "pinch": "none" }
```

## Troubleshooting

**`pip` is not recognized.** Use `python -m pip install ...` instead.

**`module 'mediapipe' has no attribute 'solutions'`.** You are using an old copy of the script. NOVA v2 handles this automatically.

**`No matching distribution found for mediapipe`.** Your Python is too new or too old for that version. Use Python 3.11 or 3.12, or run `python -m pip install --upgrade mediapipe`.

**Volume control unavailable.** Run `python -m pip install pycaw comtypes`.

**Screenshot failed.** Run `python -m pip install mss pillow`.

**No tray icon.** Run `python -m pip install pystray pillow`. Hotkeys still work without it.

**Wrong camera or black picture.** Run `python nova.py --pick-camera`. Close other apps that use the camera (Zoom, Teams, browser tabs).

**A gesture triggers by accident or does not trigger.** Run `python nova.py --debug` to see what NOVA detects, then raise or lower `hold_ms` in `nova_config.json`.

**NOVA says it is already running.** Quit it with Ctrl + Alt + Q or the tray icon, then start again.

## Project files

```
nova.py              main program
requirements.txt     Python dependencies
nova_config.json     settings (created on first run)
hand_landmarker.task hand model (downloaded on first run, newer MediaPipe only)
test_nova_logic.py   tests for the gesture logic
```

## Tech

Python, OpenCV, Google MediaPipe, pycaw (Windows audio), mss / Pillow (screenshots), pystray (tray icon).

## Author

Premdeep Arora
