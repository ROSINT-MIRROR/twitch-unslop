#!/usr/bin/env python3
"""
Drive LibreWolf at a channel and just sit there while the tap records.

  python driver/drive.py <channel> [minutes]

Uses the in-project profile IN PLACE (-profile <abs>), so geckodriver does not
clone it into /tmp and your login/sub state persists between runs.
"""
import os
import sys
import time
import pathlib

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

LAB = pathlib.Path(__file__).resolve().parents[1]
PROFILE = LAB / "browser" / "profile"
LOGS = LAB / "data" / "logs"
SCRATCH = LAB / "data" / ".scratch"
LOGS.mkdir(parents=True, exist_ok=True)
SCRATCH.mkdir(parents=True, exist_ok=True)

# keep every stray temp file inside the lab
os.environ["TMPDIR"] = str(SCRATCH)

# /usr/bin/librewolf is a /bin/sh wrapper; geckodriver needs the real ELF
BINARY = os.environ.get("LIBREWOLF") or next(
    (c for c in ("/usr/lib/librewolf/librewolf", "/opt/librewolf/librewolf")
     if os.path.isfile(c)), "/usr/bin/librewolf")


def main():
    channel = sys.argv[1] if len(sys.argv) > 1 else "twitch"
    minutes = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    if not (PROFILE / "cert9.db").exists():
        sys.exit("!! run browser/setup-profile.sh first")

    opts = Options()
    opts.binary_location = BINARY
    # in-place profile: no temp clone, cookies survive
    opts.add_argument("-profile")
    opts.add_argument(str(PROFILE))

    svc = Service(
        executable_path="/usr/bin/geckodriver",
        log_output=str(LOGS / "geckodriver.log"),
        # belt-and-braces: any profile geckodriver *does* make stays here
        service_args=["--profile-root", str(SCRATCH)],
    )

    drv = webdriver.Firefox(options=opts, service=svc)
    try:
        url = f"https://www.twitch.tv/{channel}"
        print(f"[*] {url}  for {minutes} min")
        drv.get(url)

        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            time.sleep(30)
            try:
                # cheap liveness probe; also proves the player is still attached
                st = drv.execute_script(
                    "const v=document.querySelector('video');"
                    "return v?{t:v.currentTime,paused:v.paused,rs:v.readyState}:null;"
                )
                print(f"[.] {int(deadline-time.time())}s left  video={st}", flush=True)
            except Exception as e:
                print(f"[!] probe failed: {e}", flush=True)
    finally:
        drv.quit()


if __name__ == "__main__":
    main()
