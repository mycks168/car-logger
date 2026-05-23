"""voice-assistant エントリポイント。"""

import logging
import signal
import sys

from core.app import Assistant

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/voice-assistant.log", mode="a"),
    ],
)


def main():
    assistant = Assistant()

    def _sigterm_handler(signum, frame):
        assistant.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _sigterm_handler)
    assistant.run()


if __name__ == "__main__":
    main()
