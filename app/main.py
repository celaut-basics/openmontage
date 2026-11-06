"""OpenMontage as a Celaut service: the chat interface of chatui (chat.md revision 6) on the
port of the slot; the answers are plans and videos made with OpenMontage's free tools.

Run as `python3 -m app.main` from the directory that holds `app/`.
"""

import logging
import os
import signal
import sys
import threading

from . import egress
from .chat import Chat
from .director import Director
from .llm import LLM
from .producer import Producer
from .server import make_server


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    hosts = egress.install()
    llm = LLM()
    producer = Producer()
    producer.work_root.mkdir(parents=True, exist_ok=True)
    upstream = os.environ.get("OPENMONTAGE_UPSTREAM_COMMIT", "unknown")
    chat = Chat(Director(llm, producer, upstream))
    server, port = make_server(chat, port=int(os.environ.get("PORT", "8080")))
    server.start()
    logging.info("openmontage: gRPC on port %d; LLM %s; upstream %s; egress %s",
                 port, llm.describe(), upstream[:12], ", ".join(hosts))

    done = threading.Event()

    def stop(signum, frame):
        logging.info("signal %d: stop", signum)
        server.stop(5)
        done.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    done.wait()


if __name__ == "__main__":
    main()
