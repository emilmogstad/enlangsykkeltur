"""Railway start command: serve the Streamlit dashboard on $PORT."""

import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def main():
    # Keep the SQLite database on the Railway volume (if attached) so it survives redeploys.
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    if volume and not os.environ.get("RACE_APP_DB"):
        os.environ["RACE_APP_DB"] = str(Path(volume) / "races_v2.sqlite3")
    os.chdir(APP_DIR)
    command = [
        sys.executable, "-m", "streamlit", "run", "race_application_app.py",
        "--server.address", "0.0.0.0",
        "--server.port", os.environ.get("PORT", "8501"),
        "--server.headless", "true",
        "--browser.gatherUsageStats", "false",
    ]
    if os.name == "nt":
        import subprocess
        sys.exit(subprocess.call(command))
    os.execv(sys.executable, command)


if __name__ == "__main__":
    main()
