# Mechanical-Keyboard-Simulator-v7.0
terminal-based mechanical keyboard simulator that reproduces realistic key and mouse sounds with near-zero latency. Supports customizable key bindings, adjustable volume, repeat mode, and multiple DSP presets for authentic typing feedback. The sensation of pressing a different key from a single audio file.


## 📦 setup
* install latest python from here "https://www.python.org/"
* install all of this python libraries: "pygame pynput numpy scipy"
* install my project from the releases tab
* Create a folder on your computer — this folder will be where your default mouse and keyboard sounds should be located. Then, place the sound files into that folder: **mouse_tik.wav** (the effect sound file you want to assign to your mouse) and **normal_tik.wav** (the effect sound file you want to assign to your keyboard) If you don’t want to add default sounds for any device, you can add an empty .wav file or simply not add anything at all (it will be recorded as an error in the logs if used this way). You can find and download these from somewhere on the internet or simply download from **[here](https://github.com/ArdaGral06/Mechanical-Keyboard-Simulator-v7.0/tree/main/DefaultSoundLibrary).** If your sound files are not in .wav format, you can convert them to .wav here: "https://convertio.co/en/."
* If you want to trim certain parts of the sound file you found, this site will help: https://audiotrimmer.com/
. Remember, your sound files must always be .wav for the lowest latency and minimum system requirements; otherwise, the application will give an error. Once you have done all this, move to the next step: open the config.json file and replace this part with the directory of the folder you created: "dir" : "C:/Users/TYPE_YOUR_DIRECTION", tip: if you are using Windows 11, you can right-click the folder and choose "Copy as path". Important note: make sure your directory starts with "**/**" instead of "**\(backslash)**"; otherwise, you will get an error. Yes, that was the setup part, you can find the other necessary information below.

## ❓ How To Run It
* Simple way: find your code folders (folders name should be: MechanicalKeyboardApp)  right click on it and click "**Open in Windows Terminal**" Or:
* open powershell from your computer
* type cd C:\Users\Enter_the_directory_of_the_folder_containing_the_code_you_downloaded_from_my_GitHub_repo
* then type **py "main.py"** and there you go code is working now.

## 🕹️ How To Use
* if you want to set volume type 0 - 1 // 0 = 0% / 1 = 100%
* if you want to create a custom key sound effects for a key, press **c** and you got a user interface. type 1 to select single .wav file, if you select then press which key you want to set the selected .wav sound effect. If you want add multiple .wav file and set multiple key sound type 2 select an folder and the folder should contain key names example: if you want to add space key sound effect and enter sound effect you need to add in the folders "space.wav" file for space key and "enter.wav" to enter key. // new feature: When you download sound packs labeled as config-v1 from the official https://mechvibes.com/sound-packs/ website and select the downloaded folder(unzip pls), the system will automatically detect and configure them. Saved key bindings are always stored in the key_bindings.json file.
If you wish to remove them, you can delete your saved custom key sets directly from this file (do not delete the file open the file and delete all lines and save it). And last one you can cancel with 0. """NOTE: If the file selection screen doesn't appear, you can type the path of the folder or file."""
* if you want to keyboard sound effect play continuously press **r** and enter therefore, as long as you hold down the button, the sound effect will continuously repeat itself.
* and last one if you want to close the code you can type just **q** or **exit** you can also use a classic way press **ctrl+c**.

## 📈 System Performance

- **Audio Latency:** ~11 ms
- **CPU Usage:** Low; depends on the number of simultaneous sounds (polyphony)
- **RAM Usage:** Minimal; only a few MB for sound pools
- **Supported OS:** Windows, macOS, Linux (Python 3.9+ recommended)
