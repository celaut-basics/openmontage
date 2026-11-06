"""The conversation of the service: the history, the files and the answers.

chat.md revision 6 (celaut-project/chatui, docs/celaut/chat.md). The structure is the one
of celaut-basics/echo-chat (service/store.py) and celaut-basics/distributed-llama
(head/app/chat.py): everything that changes the history, and the event that tells about
the change, happen under one lock. So a `Connect` stream that joins sees a replay and then
the live events, with none lost and none sent twice (section 6.5, step 3).

What this service adds:

- The user sends text only (`accepted_mime_types` is empty). The service sends files: the
  video (`file`, video/mp4), a poster (`image`, image/jpeg) and the sound track (`audio`,
  audio/mpeg). So `FileService` is implemented for `QueryFiles` and `DownloadFile`;
  `UploadFile` gets `unsupported_media` (section 6.4).
- `max_file_bytes` is 24 MiB: the largest file that the service sends. The video is made
  to stay below it.
- One answer can take minutes (a render). The answer is streamed (progress lines as text
  deltas, and `MessageProgress` events). A part that is not text cannot be added to an
  open message (section 5.2), so the files come in a second assistant message, a whole
  answer, with the same `reply_to_message_id`.
- The title of the conversation (section 6.10) and suggestions (section 5.7).
"""

import hashlib
import os
import queue
import re
import threading
import time
import uuid

from . import chat_pb2 as pb

SHA3_256_TYPE = hashlib.sha3_256(b"").digest()  # chat.md section 6.4

MAX_TEXT = 16_384
MAX_REQUEST = 128 << 10
MAX_FILE = 24 << 20  # the largest file that the service sends (GetCapabilities)
MAX_FILES = 4
READ_LIMIT = 1000  # the most messages of one GetMessages
MAX_ERROR = 1024
MAX_TITLE = 256
MAX_SUGGESTION = 64
CHUNK = 64 << 10
KEY = re.compile("[A-Za-z0-9_-]{1,64}")
CONTROL = re.compile("[\x00-\x1f\x7f]")
STATUS = pb.MessageStatus
OPEN = (STATUS.MESSAGE_STATUS_PENDING, STATUS.MESSAGE_STATUS_IN_PROGRESS)
MAX_DELTA = 60_000  # a delta has 64 KiB or less (section 5.2)


class Reject(Exception):
    """A call that fails. `status` is a name of `grpc.StatusCode`, `reason` goes in the
    trailer `chat-reason` (chat.md section 9)."""

    def __init__(self, status, reason, message=""):
        super().__init__(message or reason)
        self.status, self.reason, self.message = status, reason, message or reason


def bad_request(message):
    return Reject("INVALID_ARGUMENT", "bad_request", message)


def normalize_mime(mime):
    return mime.split(";")[0].strip().lower()


def text_of(content):
    return "".join(p.text.text for p in content if p.WhichOneof("kind") == "text")


def cut_utf8(text, limit):
    return text.encode()[:limit].decode("utf-8", "ignore")


def cut_error(text):
    """`error` is UTF-8 of 1 to 1 024 bytes, and no character is cut."""
    return cut_utf8(text or "the answer failed", MAX_ERROR) or "the answer failed"


def one_line(text, limit):
    """A title (`limit` UTF-8 bytes) or a suggestion (`limit` characters): one line, with
    no control character (sections 5.7 and 6.10)."""
    text = " ".join(CONTROL.sub(" ", text or "").split())
    return cut_utf8(text, limit).strip() if limit == MAX_TITLE else text[:limit].strip()


def sha3_file(path):
    digest = hashlib.sha3_256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.digest()


class Chat:
    """One main conversation. `agent` makes the answers (see `director.Director`): its
    `answer(turn)` gets a `Turn` and uses it to stream text, report progress, post files
    and end the answer."""

    def __init__(self, agent, max_messages=None, max_queue=None):
        env = os.environ.get
        self.agent = agent
        self.max_messages = int(env("CHAT_MAX_MESSAGES", 2_000)) if max_messages is None else max_messages
        self.max_queue = int(env("CHAT_MAX_QUEUE", 8)) if max_queue is None else max_queue
        self.created_at = self._now()
        self.messages = []  # sequence n is messages[n - 1]
        self.by_id = {}
        self.keys = {}  # idempotency_key -> message_id
        self.files = {}  # SHA3-256 value -> (path, size)
        self.subscribers = []
        self.pending = 0  # user messages that wait for an answer
        self.cancelled = set()  # message_id of the answers that the client cancelled
        self.title = None
        self.title_at = 0
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def close(self):
        self.jobs.put(None)

    @staticmethod
    def _now():
        return time.time_ns() // 1_000_000

    @property
    def latest(self):
        return len(self.messages)

    # The next methods run with the lock held.

    def _touch(self, message):
        message.updated_at = max(self._now(), message.updated_at)

    def _publish(self, **event):
        event = pb.ServerEvent(**event)
        for q in self.subscribers:
            q.put(event)

    def _add(self, **fields):
        now = self._now()
        message = pb.Message(message_id=uuid.uuid4().hex, sequence=self.latest + 1,
                             created_at=now, updated_at=now, **fields)
        self.messages.append(message)
        self.by_id[message.message_id] = message
        self._publish(message_created=pb.MessageCreated(message=message))
        return message

    def _set_status(self, message, status, error=None, suggestions=()):
        message.status = status
        if error is not None:
            message.error = error
        if suggestions:
            message.suggestions.extend(suggestions)
        self._touch(message)
        self._publish(message_status_changed=pb.MessageStatusChanged(
            message_id=message.message_id, status=status, updated_at=message.updated_at,
            error=error, suggestions=list(suggestions)))

    def _pin(self, message, pinned):
        if message.pinned == pinned:
            return
        message.pinned = pinned
        self._touch(message)
        self._publish(message_pinned_changed=pb.MessagePinnedChanged(
            message_id=message.message_id, pinned=pinned, updated_at=message.updated_at))

    # Calls.

    def capabilities(self):
        # The user sends text only (accepted_mime_types empty); the files of the service
        # are up to MAX_FILE.
        return pb.Capabilities(accepted_mime_types=[], max_file_bytes=MAX_FILE,
                               supports_fork=False, supports_cancel=True)

    def conversation(self):
        with self.lock:
            return pb.Conversation(conversation_id="", created_at=self.created_at,
                                   archived=False, title=self.title)

    def send(self, request):
        """chat.md section 6.1. Return (user message, True if it is a repeat)."""
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        # Step 1: the structure.
        if not request.content:
            raise bad_request("content has no part")
        attachments = []
        for part in request.content:
            kind = part.WhichOneof("kind")
            if kind not in ("text", "image", "audio"):
                raise bad_request("a part needs a kind that a client can send")
            if kind != "text":
                attachments.append((kind, getattr(part, kind).file))
        if request.idempotency_key and not KEY.fullmatch(request.idempotency_key):
            raise bad_request("idempotency_key has 1 to 64 characters of A-Za-z0-9_-")
        text = text_of(request.content)
        if not text and not attachments:
            raise bad_request("the text is empty and there is no attachment")
        for kind, ref in attachments:
            if not ref.hashes or any(not h.type for h in ref.hashes):
                raise bad_request("each hash of a file needs a type")
            if any(h.type == SHA3_256_TYPE and len(h.value) != 32 for h in ref.hashes):
                raise bad_request("a SHA3-256 hash has 32 bytes")
            if not ref.mime_type or ref.size_bytes < 1:
                raise bad_request("a file needs mime_type and size_bytes of 1 or more")
            mime = normalize_mime(ref.mime_type)
            if (mime.startswith("image/") and kind != "image") or (
                    mime.startswith("audio/") and kind != "audio"):
                raise bad_request("the kind of the part does not agree with mime_type")
        if len(attachments) > MAX_FILES:
            raise bad_request("a message has %d attachments or less" % MAX_FILES)
        # Step 2: the size.
        if (len(text.encode()) > MAX_TEXT or request.ByteSize() > MAX_REQUEST
                or any(ref.size_bytes > MAX_FILE for _, ref in attachments)):
            raise Reject("INVALID_ARGUMENT", "too_large", "the text, the request or a file is too large")
        # Step 3: the type. The user sends text only.
        if attachments:
            raise Reject("INVALID_ARGUMENT", "unsupported_media", "describe the video in text")
        with self.lock:
            # Step 4.
            if request.reply_to_message_id and request.reply_to_message_id not in self.by_id:
                raise Reject("NOT_FOUND", "not_found", "reply_to_message_id is not a message")
            # Step 5 has nothing to check: there is no attachment.
            # Step 6.
            if request.idempotency_key and request.idempotency_key in self.keys:
                known = self.by_id[self.keys[request.idempotency_key]]
                return pb.Message.FromString(known.SerializeToString()), True
            # Steps 7 and 8. Each message that waits adds its answers (2 for a render).
            if self.latest + 3 * (self.pending + 1) > self.max_messages:
                raise Reject("RESOURCE_EXHAUSTED", "history_full", "start a new instance")
            if self.pending >= self.max_queue:
                raise Reject("UNAVAILABLE", "busy", "try again later")
            content = [pb.ContentPart.FromString(p.SerializeToString()) for p in request.content]
            corrector = request.corrector if request.HasField("corrector") else None
            user = self._add(content=content, status=STATUS.MESSAGE_STATUS_COMPLETED,
                             reply_to_message_id=request.reply_to_message_id or None,
                             idempotency_key=request.idempotency_key or None,
                             corrector=corrector)
            self.pending += 1
            if user.idempotency_key:
                self.keys[user.idempotency_key] = user.message_id
            # One answer at a time (a render uses all the CPUs): the answer is PENDING
            # from the send.
            answer = self._add(status=STATUS.MESSAGE_STATUS_PENDING, from_service=True,
                               reply_to_message_id=user.message_id)
            self.jobs.put({"user": user.message_id, "answer": answer.message_id})
            return pb.Message.FromString(user.SerializeToString()), False

    def get_messages(self, request):
        """chat.md section 6.2. Return a list of messages."""
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        if request.limit and request.last:
            raise bad_request("limit and last cannot go together")
        with self.lock:
            if request.after_sequence > self.latest:
                raise bad_request("after_sequence is more than latest")
            first = request.after_sequence + 1
            end = self.latest if not request.before_sequence else min(self.latest, request.before_sequence - 1)
            if end < first:
                return []
            if request.last:
                first = max(first, end - min(request.last, READ_LIMIT) + 1)
            else:
                end = min(end, first + (request.limit or READ_LIMIT) - 1, first + READ_LIMIT - 1)
            return [pb.Message.FromString(m.SerializeToString()) for m in self.messages[first - 1:end]]

    def set_pinned(self, request):
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        with self.lock:
            message = self.by_id.get(request.message_id)
            if message is None:
                raise Reject("NOT_FOUND", "not_found", "there is no such message")
            self._pin(message, request.pinned)
            return pb.Message.FromString(message.SerializeToString())

    def cancel(self, request):
        """`cancel_message` of `Connect` (section 6.5)."""
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        with self.lock:
            message = self.by_id.get(request.message_id)
            if message is None:
                raise Reject("NOT_FOUND", "not_found", "there is no such message")
            if message.status not in OPEN:
                raise Reject("FAILED_PRECONDITION", "bad_state", "the message is final")
            # The work stops at its next check; the status is final now.
            self.cancelled.add(message.message_id)
            self._set_status(message, STATUS.MESSAGE_STATUS_CANCELLED)

    def answer_secret(self, request):
        """Section 6.9. The service sends no secret request, so no message has one."""
        raise Reject("NOT_FOUND", "not_found", "there is no such secret request")

    def subscribe(self, events, after_sequence):
        """Put the replay and `SubscriptionReady` in `events`, then the live events."""
        with self.lock:
            if after_sequence > self.latest:
                raise bad_request("after_sequence is more than latest")
            for message in self.messages[after_sequence:]:
                events.put(pb.ServerEvent(message_created=pb.MessageCreated(message=message)))
            events.put(pb.ServerEvent(subscription_ready=pb.SubscriptionReady(
                latest_sequence=self.latest, title=self.title)))
            self.subscribers.append(events)

    def unsubscribe(self, events):
        with self.lock:
            if events in self.subscribers:
                self.subscribers.remove(events)

    def repeat(self, events, message):
        """The `MessageCreated` of a send that was a repeat goes to the stream of the repeat."""
        events.put(pb.ServerEvent(message_created=pb.MessageCreated(message=message)))

    def check_acknowledge(self, last_sequence):
        with self.lock:
            if last_sequence > self.latest:
                raise bad_request("last_sequence is more than latest")

    # Files (section 6.4).

    def file_path(self, value):
        with self.lock:
            entry = self.files.get(value)
            return entry[0] if entry else None

    def add_file(self, path, mime, filename=None):
        """Keep a file of the service until the instance stops. Return its FileReference."""
        value = sha3_file(path)
        size = os.path.getsize(path)
        if size < 1 or size > MAX_FILE:
            raise ValueError("a file of the service has 1 to %d bytes, not %d" % (MAX_FILE, size))
        with self.lock:
            self.files[value] = (path, size)
        return pb.FileReference(hashes=[pb.Hash(type=SHA3_256_TYPE, value=value)],
                                mime_type=mime, size_bytes=size, filename=filename)

    # The title (section 6.10).

    def set_title(self, title):
        title = one_line(title, MAX_TITLE) or None
        with self.lock:
            if title == self.title:
                return
            self.title = title
            self.title_at = max(self._now(), self.title_at)
            self._publish(conversation_title_changed=pb.ConversationTitleChanged(
                title=title, updated_at=self.title_at))

    # The answers.

    def history(self, user_id):
        """The turns up to the user message `user_id`: user texts and the text of the
        final answers, in order of sequence."""
        turns = []
        with self.lock:
            last = self.by_id[user_id].sequence
            for m in self.messages[:last]:
                if not m.from_service:
                    turns.append(("user", text_of(m.content)))
                elif m.status == STATUS.MESSAGE_STATUS_COMPLETED:
                    turns.append(("assistant", text_of(m.content)))
        return turns

    def _work(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            turn = Turn(self, job["user"], job["answer"])
            try:
                self.agent.answer(turn)
                turn.finish()  # an answer must end (section 5.2)
            except Cancelled:
                pass
            except Exception as error:  # noqa: BLE001 (the answer must end)
                turn.fail("the service failed: %s" % (error,))
            finally:
                with self.lock:
                    self.pending -= 1
                    self.cancelled.discard(job["answer"])


class Cancelled(Exception):
    """The client cancelled the answer; the work stops."""


class Turn:
    """The answer to one user message, as the agent sees it."""

    def __init__(self, chat, user_id, answer_id):
        self.chat, self.user_id, self.answer_id = chat, user_id, answer_id
        with chat.lock:
            self.user_text = text_of(chat.by_id[user_id].content)

    def history(self):
        return self.chat.history(self.user_id)

    def cancelled(self):
        with self.chat.lock:
            return self.answer_id in self.chat.cancelled

    def check(self):
        if self.cancelled():
            raise Cancelled()

    def _answer(self):
        return self.chat.by_id[self.answer_id]

    def start(self):
        """PENDING to IN_PROGRESS. Raise Cancelled if the client cancelled the answer."""
        with self.chat.lock:
            answer = self._answer()
            if answer.status == STATUS.MESSAGE_STATUS_PENDING:
                self.chat._set_status(answer, STATUS.MESSAGE_STATUS_IN_PROGRESS)
                return
            if answer.status != STATUS.MESSAGE_STATUS_IN_PROGRESS:
                raise Cancelled()

    def write(self, text):
        """Add text at the end of the answer (deltas)."""
        self.start()
        while text:
            head, text = _split_utf8(text, MAX_DELTA)
            with self.chat.lock:
                answer = self._answer()
                if answer.status != STATUS.MESSAGE_STATUS_IN_PROGRESS:
                    raise Cancelled()
                if answer.content and answer.content[-1].WhichOneof("kind") == "text":
                    answer.content[-1].text.text += head
                else:
                    answer.content.add().text.text = head
                self.chat._publish(message_delta=pb.MessageDelta(
                    message_id=self.answer_id, text_append=head))

    def progress(self, fraction, description):
        """`MessageProgress` (section 6.5, for information)."""
        with self.chat.lock:
            if self._answer().status not in OPEN:
                return
            self.chat._publish(message_progress=pb.MessageProgress(
                message_id=self.answer_id, progress=max(0.0, min(1.0, fraction)),
                description=one_line(description, 200)))

    def finish(self, suggestions=()):
        """End the answer with COMPLETED (nothing if it is final already)."""
        suggestions = [s for s in (one_line(s, MAX_SUGGESTION) for s in suggestions) if s][:3]
        with self.chat.lock:
            answer = self._answer()
            if answer.status == STATUS.MESSAGE_STATUS_PENDING:
                self.chat._set_status(answer, STATUS.MESSAGE_STATUS_IN_PROGRESS)
            if answer.status == STATUS.MESSAGE_STATUS_IN_PROGRESS:
                if not answer.content:
                    answer.content.add().text.text = "Done."
                    self.chat._publish(message_delta=pb.MessageDelta(
                        message_id=self.answer_id, text_append="Done."))
                self.chat._set_status(answer, STATUS.MESSAGE_STATUS_COMPLETED,
                                      suggestions=suggestions)

    def fail(self, error):
        with self.chat.lock:
            answer = self._answer()
            if answer.status in OPEN:
                self.chat._set_status(answer, STATUS.MESSAGE_STATUS_FAILED, cut_error(error))

    def post(self, text, files=(), suggestions=(), metadata=None):
        """A second assistant message, whole and COMPLETED, with files. `files` is a list
        of (kind, path, mime, filename, extra): kind is file, image or audio; extra holds
        alt_text or duration_ms."""
        content = [pb.ContentPart(text=pb.TextContent(text=text))] if text else []
        for kind, path, mime, filename, extra in files:
            ref = self.chat.add_file(path, mime, filename)
            if kind == "image":
                content.append(pb.ContentPart(image=pb.ImageContent(file=ref, alt_text=extra.get("alt_text"))))
            elif kind == "audio":
                content.append(pb.ContentPart(audio=pb.AudioContent(
                    file=ref, duration_ms=extra.get("duration_ms"), transcript=extra.get("transcript"))))
            else:
                content.append(pb.ContentPart(file=ref))
        suggestions = [s for s in (one_line(s, MAX_SUGGESTION) for s in suggestions) if s][:3]
        with self.chat.lock:
            message = self.chat._add(content=content, status=STATUS.MESSAGE_STATUS_COMPLETED,
                                     from_service=True, reply_to_message_id=self.user_id,
                                     suggestions=suggestions,
                                     metadata={k: str(v) for k, v in (metadata or {}).items()})
            return message.message_id

    def set_title(self, title):
        self.chat.set_title(title)


def _split_utf8(text, limit):
    """(head, rest): head has `limit` UTF-8 bytes or less, and no character is cut."""
    if len(text.encode()) <= limit:
        return text, ""
    head = text.encode()[:limit].decode("utf-8", "ignore")
    return head, text[len(head):]
