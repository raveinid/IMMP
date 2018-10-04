"""
Connect to an IRC server.

Config:
    server:
        host (str):
            Hostname of the server.
        port (int):
            Non-SSL port of the server.
        ssl (bool):
            Whether to connect using SSL.
        password (str):
            Optional password required for the server.
    user:
        nick (str):
            Primary nick for the bot user.
        real-name (str):
            Real name, as displayed in WHO queries.
"""

from asyncio import Condition, ensure_future, open_connection, sleep
import logging
import re
import time

from voluptuous import ALLOW_EXTRA, Any, Optional, Schema

import immp


log = logging.getLogger(__name__)


class _Schema():

    config = Schema({"server": {"host": str,
                                "port": int,
                                Optional("ssl", default=False): bool,
                                Optional("password", default=None): Any(str, None)},
                     "user": {"nick": str,
                              "real-name": str}},
                    extra=ALLOW_EXTRA, required=True)


class Line:
    """
    Low-level representation of an IRC message, either sent or received.  Calling :func:`str` on a
    line will encode it suitable for transmission.

    Attributes:
        command (str):
            IRC verb or numeric.
        args (str list):
            Additional arguments for this message.
        source (str list):
            Optional source of this message.
        tags (dict):
            Any tags attached to the message.
    """

    _format = re.compile("(?:@(?P<tags>[a-z0-9-]+(?:=[^; ]+)?(?:;[a-z0-9-]+(?:=[^; ]+)?)*) +)?"
                         "(?::(?P<source>[^ ]+) +)?(?P<command>[a-z]+|[0-9]{3})"
                         "(?P<args>(?: +[^: ][^ ]*)*)(?: +:(?P<trailing>.*))?", re.I)

    _last_ts = 0

    def __init__(self, command, *args, source=None, tags=None):
        self.command = command
        self.args = args
        self.source = source
        self.tags = tags

    @classmethod
    def now(cls):
        """
        Generate a timestamp suitable for use as a TS value.  If called twice in quick succession,
        the value is guaranteed to be unique each time.

        Returns:
            str:
                Current timestamp in seconds.
        """
        ts = max(cls._last_ts + 1, int(time.time()))
        cls._last_ts = ts
        return str(ts)

    @classmethod
    def parse(cls, line):
        """
        Take a raw IRC line and decode it into a :class:`.Line`.

        Args:
            line (str):
                Raw IRC message.

        Returns:
            .Line:
                Parsed line.
        """
        match = cls._format.match(line)
        if not match:
            raise ValueError("Invalid line: '{}'".format(line))
        tagpart, source, command, argpart, trailing = match.groups()
        tags = {}
        args = []
        if tagpart:
            for item in tagpart.split(";"):
                key, *val = item.split("=", 1)
                tags[key] = val[0] if val else True
        if argpart:
            args = argpart.split()
        if trailing:
            args.append(trailing)
        return cls(command, *args, source=source, tags=tags)

    def __str__(self):
        line = self.command
        if self.source:
            line = ":{} {}".format(self.source, line)
        if self.tags:
            tagpart = []
            for key, value in self.tags:
                tagpart.append(key if value is True else "{}={}".format(key, value))
            line = "@{} {}".format(";".join(tagpart), line)
        if self.args:
            line = " ".join([line, *self.args[:-1], ":{}".format(self.args[-1])])
        return line

    def __repr__(self):
        return "<{}: {}{}{}>".format(self.__class__.__name__, self.command,
                                     " @ {}".format(repr(self.source)) if self.source else "",
                                     " {}".format(repr(list(self.args))) if self.args else "")


class IRCSegment(immp.Segment):
    """
    Plug-friendly representation of IRC message formatting.
    """

    @classmethod
    def to_formatted(cls, segment):
        """
        Convert a :class:`.Segment` into text formatted using IRC ASCII escape sequences.

        Args:
            segment (.Segment)
                Message segment created by another plug.

        Returns:
            str:
                Code-formatted string.
        """
        text = segment.text
        if segment.bold:
            text = "\x02{}\x02".format(text)
        if segment.italic:
            text = "\x1d{}\x1d".format(text)
        if segment.underline:
            text = "\x1f{}\x1f".format(text)
        if segment.strike:
            # Muted text by colouring it grey.  Includes a default background colour to avoid
            # accidental combinations with a literal comma in a following segment.
            text = "\x0314{}\x0399,99".format(text)
        return text


class IRCRichText(immp.RichText):
    """
    Wrapper for IRC-specific encoding of formatting.
    """

    @classmethod
    def to_formatted(cls, rich):
        """
        Convert a :class:`.RichText` into text formatted using IRC ASCII escape sequences.

        Args:
            rich (.RichText):
                Parsed rich text container.

        Returns:
            str:
                Code-formatted string.
        """
        return "".join(IRCSegment.to_formatted(segment) for segment in rich).replace("\t", " ")


class IRCMessage(immp.Message):
    """
    Message originating from IRC.
    """

    @classmethod
    def from_line(cls, irc, line):
        """
        Convert a :class:`.Line` into a :class:`.Message`.

        Args:
            irc (.IRCPlug):
                Related plug instance that provides the line.
            line (.Line):
                Raw message line from the server.

        Returns:
            .IRCMessage:
                Parsed message object.
        """
        channel = line.args[0]
        nick = line.source.split("!", 1)[0]
        if channel == irc.config["user"]["nick"]:
            # Private messages arrive "from A to B", and should be sent "from B to A".
            channel = nick
        user = immp.User(id=line.source, plug=irc, username=nick, raw=line)
        action = False
        joined = []
        left = []
        if line.command == "JOIN":
            text = "joined {}".format(channel)
            action = True
            joined.append(user)
        elif line.command == "PART":
            text = "left {}".format(channel)
            action = True
            left.append(user)
        else:
            text = line.args[1]
            match = re.match(r"\x01ACTION ([^\x01]*)\x01", text)
            if match:
                text = match.group(1)
                action = True
        return immp.SentMessage(id=Line.now(),
                                channel=immp.Channel(irc, channel),
                                user=user,
                                text=text,
                                action=action,
                                joined=joined,
                                left=left,
                                raw=line)


class IRCPlug(immp.Plug):
    """
    Plug for an IRC server.
    """

    def __init__(self, name, config, host):
        super().__init__(name, _Schema.config(config), host)
        self._reader = self._writer = None
        # Bot's own identifier as seen by the IRC server.
        self._source = None
        # Tracking fields for storing incoming data by type.
        self._waits = []
        self._data = {}
        # Don't yield messages for initial self-joins.
        self._joins = set()

    @property
    def network_name(self):
        return "{} IRC".format(self.config["server"]["host"])

    @property
    def network_id(self):
        return "irc:{}".format(self.config["server"]["host"])

    async def start(self):
        host = self.config["server"]["host"]
        port = self.config["server"]["port"]
        ssl = self.config["server"]["ssl"] or None
        reader, self._writer = await open_connection(host, port, ssl=ssl)
        self._reader = ensure_future(self._read_loop(reader, host, port))
        if self.config["server"]["password"]:
            self.write(Line("PASS", self.config["server"]["password"]))
        self.write(Line("NICK", self.config["user"]["nick"]),
                   Line("USER", "immp", "0", "*", self.config["user"]["real-name"]))
        # We won't receive this until a valid nick has been set.
        await self.wait("001")
        self._source = (await self.user_from_username(self.config["user"]["nick"])).id
        for channel in self.host.channels.values():
            if channel.plug == self and channel.source.startswith("#"):
                self._joins.add(channel.source)
                self.write(Line("JOIN", channel.source))

    async def stop(self):
        if self._reader:
            log.debug("Closing reader")
            self._reader.cancel()
            self._reader = None
        if self._writer:
            log.debug("Closing writer")
            self._writer.close()
            self._writer = None
        self._source = None
        self._waits.clear()
        self._data.clear()
        self._joins.clear()

    async def _read_loop(self, reader, host, port):
        while True:
            raw = await reader.readline()
            if not raw:
                # Connection has been closed.
                self._writer.close()
                self._reader = self._writer = None
                break
            line = Line.parse(raw.decode().rstrip("\r\n"))
            log.debug("Received line: {}".format(repr(line)))
            await self.handle(line)
        log.debug("Reconnecting in 3 seconds")
        await sleep(3)
        await self.start()

    async def _who(self, name):
        self.write(Line("WHO", name))
        data = await self.wait("315", collect=("352",))
        users = []
        for line in data["352"]:
            id = "{}!{}@{}".format(line.args[5], line.args[2], line.args[3])
            users.append(immp.User(id=id, plug=self, username=line.args[5], raw=line))
        return users

    async def user_from_id(self, id):
        nick = id.split("!", 1)[0]
        return immp.User(id=id, plug=self, username=nick)

    async def user_from_username(self, username):
        for user in await self._who(username):
            if user.username == username:
                return user
        return None

    async def channel_for_user(self, user):
        return immp.Channel(self, user.username)

    async def channel_is_private(self, channel):
        return bool(await self.user_from_username(channel.source))

    async def channel_members(self, channel):
        return await self._who(channel.source)

    async def handle(self, line):
        if line.command in self._data:
            self._data[line.command].append(line)
        for commands, cond in self._waits:
            if line.command in commands:
                async with cond:
                    cond.notify_all()
        if line.command == "PING":
            self.write(Line("PONG", *line.args))
        elif line.command in ("JOIN", "PART", "PRIVMSG"):
            sent = IRCMessage.from_line(self, line)
            if sent.joined and sent.joined[0].username == self._source.split("!", 1)[0]:
                if sent.channel.source in self._joins:
                    self._joins.remove(sent.channel.source)
                    return
            self.queue(sent)
        elif line.command == "433":
            # Nickname in use, try another.
            self.config["user"]["nick"] += "_"
            self.write(Line("NICK", self.config["user"]["nick"]),
                       Line("USER", "immp", "0", "*", self.config["user"]["real-name"]))

    async def wait(self, success, fail=(), collect=()):
        # Add lists to capture data as it comes in.
        if any(command in self._data for command in fail + collect):
            raise RuntimeError("Already listening for collected commands")
        for command in fail + collect:
            self._data[command] = []
        # Block until we receive the response code we're looking for.
        cond = Condition()
        pair = (((success,) + tuple(fail)), cond)
        self._waits.append(pair)
        async with cond:
            await cond.wait()
        self._waits.remove(pair)
        # Retrieve captured data for this wait.
        if any(self._data[command] for command in fail):
            raise ValueError("Received error response")
        data = {command: self._data[command] for command in collect}
        for command in fail + collect:
            del self._data[command]
        return data

    def write(self, *lines):
        for line in lines:
            log.debug("Sending line: {}".format(repr(line)))
            self._writer.write("{}\r\n".format(line).encode())

    def _lines(self, rich, user, action, edited):
        if not rich:
            return []
        elif not isinstance(rich, immp.RichText):
            rich = immp.RichText([immp.Segment(rich)])
        lines = []
        for text in IRCRichText.to_formatted(rich).split("\n"):
            if user:
                template = "* {} {}" if action else "<{}> {}"
                text = template.format(user.username or user.real_name, text)
            if edited:
                text = "[edit] {}".format(text)
            if not user and action:
                text = "\x01ACTION {}\x01".format(text)
            lines.append(text)
        return lines

    async def put(self, channel, msg):
        lines = []
        edited = isinstance(msg, immp.SentMessage) and msg.edited
        if msg.text:
            lines += self._lines(msg.text, msg.user, msg.action, edited)
        for attach in msg.attachments:
            if isinstance(attach, immp.File):
                text = "uploaded a file{}".format(": {}".format(attach) if str(attach) else "")
                lines += self._lines(text, msg.user, True, edited)
            elif isinstance(attach, immp.Location):
                text = "shared a location: {}".format(attach)
                lines += self._lines(text, msg.user, True, edited)
            elif isinstance(attach, immp.SentMessage) and attach.empty:
                pass
            elif isinstance(attach, immp.Message) and attach.text:
                lines += self._lines(attach.text, attach.user, attach.action,
                                     isinstance(attach, immp.SentMessage) and attach.edited)
        ids = []
        for text in lines:
            line = Line("PRIVMSG", channel.source, text)
            self.write(line)
            line.source = self._source
            sent = IRCMessage.from_line(self, line)
            self.queue(sent)
            ids.append(sent.id)
        return ids
