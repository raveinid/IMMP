"""
Connect to `Discord <https://discordapp.com>`_ as a bot.

Config:
    token (str):
        Discord token for the bot user.
    bot (bool):
        Whether the token represents a bot user (true by default).
    webhooks (dict):
        Mapping from Discord channel IDs to webhook URLs, needed for custom message author
        names and avatars.
    playing (str):
        Optional game activity message to show as the bot's presence.

Note that the token is neither a client ID nor client secret -- you need to enable bot features for
your app, and collect the token from there.  New apps can be created from the `My Apps
<https://discordapp.com/developers/applications/me>`_ page in the developer docs.

Because gateway connections can't customise the sender when pushing new messages, you may also want
an `incoming webhook <https://discordapp.com/developers/docs/resources/webhook>`_  configured for
each channel you intend to send messages to.  A new webhook can be created over the API, or in the
UI via Edit Channel > Webhooks.  A fallback style incorporating the user's name in the message text
will be used in lieu of a webhook, e.g. with direct messages.

.. note::
    This plug requires the **new 1.0 release** of the `discord.py
    <https://discordpy.readthedocs.io/en/rewrite/>`_ Python module, which is currently in alpha.
"""

from asyncio import Condition, ensure_future
from collections import defaultdict
from functools import partial
from json import dumps as json_dumps
import logging
import re

from aiohttp import ClientSession, FormData
import discord
from emoji import emojize
from voluptuous import ALLOW_EXTRA, Any, Optional, Schema

import immp


log = logging.getLogger(__name__)


class _Schema:

    config = Schema({"token": str,
                     Optional("bot", default=True): bool,
                     Optional("webhooks", default=dict): dict,
                     Optional("playing", default=None): Any(str, None)},
                    extra=ALLOW_EXTRA, required=True)

    webhook = Schema({"id": str}, extra=ALLOW_EXTRA, required=True)


class DiscordAPIError(immp.PlugError):
    """
    Generic error from the Discord API.
    """


class DiscordUser(immp.User):
    """
    User present in Discord.
    """

    @classmethod
    def from_user(cls, discord, user):
        """
        Convert a :class:`discord.User` into a :class:`.User`.

        Args:
            discord (.DiscordPlug):
                Related plug instance that provides the user.
            user (discord.User):
                Hangups user object retrieved from the user list.

        Returns:
            .DiscordUser:
                Parsed user object.
        """
        username = "{}#{}".format(user.name, user.discriminator)
        real_name = getattr(user, "nick", None) or user.name
        avatar = user.avatar_url or None
        return cls(id=user.id,
                   plug=discord,
                   username=username,
                   real_name=real_name,
                   avatar=avatar,
                   raw=user)


class DiscordRichText(immp.RichText):

    tags = {"**": "bold", "_": "italic", "~": "strike", "`": "code", "```": "pre"}

    @classmethod
    def _sub_channel(cls, discord, match):
        return "#{}".format(discord._client.get_channel(match.group(1)).name)

    @classmethod
    def from_markdown(cls, discord, text):
        """
        Convert a string of Markdown into a :class:`.RichText`.

        Args:
            discord (.DiscordPlug):
                Related plug instance that provides the text.
            text (str):
                Markdown formatted text.

        Returns:
            .DiscordRichText:
                Parsed rich text container.
        """
        # TODO: Full Markdown parser.
        mentions = defaultdict(dict)
        for match in re.finditer(r"<@!?(\d+)>", text):
            user = discord._client.get_user(int(match.group(1)))
            if user:
                mentions[match.start()] = DiscordUser.from_user(discord, user)
                mentions[match.end()] = None
        segments = []
        points = list(mentions.keys())
        # Iterate through text in change start/end pairs.
        for start, end in zip([0] + points, points + [len(text)]):
            if start == end:
                # Zero-length segment at the start or end, ignore it.
                continue
            if mentions[start]:
                user = mentions[start]
                part = "@{}".format(user.username or user.real_name)
            else:
                user = None
                part = emojize(text[start:end], use_aliases=True)
                # Strip Discord channel/emoji tags, replace with a plain text representation.
                part = re.sub(r"<#(\d+)>", partial(cls._sub_channel, discord), part)
                part = re.sub(r"<(:[^: ]+:)\d+>", r"\1", part)
            segments.append(immp.Segment(part, mention=user))
        return cls(segments)

    @classmethod
    def _sub_emoji(cls, discord, match):
        for emoji in discord._client.emojis:
            if emoji.name == match.group(1):
                return str(emoji)
        return ":{}:".format(match.group(1))

    @classmethod
    def to_markdown(cls, discord, rich):
        """
        Convert a :class:`.RichText` instance into a Markdown string.

        Args:
            discord (.DiscordPlug):
                Related plug instance to cross-reference users.
            rich (.DiscordRichText):
                Parsed rich text container.

        Returns:
            str:
                Markdown formatted text.
        """
        text = ""
        active = []
        for segment in rich.normalise():
            for tag in reversed(active):
                # Check all existing tags, and remove any that end at this segment.
                attr = cls.tags[tag]
                if not getattr(segment, attr):
                    text += tag
                    active.remove(tag)
            for tag, attr in cls.tags.items():
                # Add any new tags that start at this segment.
                if getattr(segment, attr) and tag not in active:
                    text += tag
                    active.append(tag)
            if segment.mention and isinstance(segment.mention.plug, DiscordPlug):
                text += "<@{}>".format(segment.mention.id)
            elif segment.link:
                text += "[{}]({})".format(segment.text, segment.link)
            else:
                text += segment.text
        for tag in reversed(active):
            # Close all remaining tags.
            text += tag
        return re.sub(r":([^: ]+):", partial(cls._sub_emoji, discord), text)


class DiscordMessage(immp.Message):
    """
    Message originating from Discord.
    """

    @classmethod
    def from_message(cls, discord, message):
        """
        Convert a :class:`discord.Message` into a :class:`.Message`.

        Args:
            discord (.DiscordPlug):
                Related plug instance that provides the event.
            message (discord.Message):
                Discord message object received from a channel.

        Returns:
            .DiscordMessage:
                Parsed message object.
        """
        text = None
        attachments = []
        if message.content:
            # TODO: Rich text.
            text = message.content
        for attach in message.attachments:
            type = immp.File.Type.unknown
            if attach.filename.rsplit(".", 1)[1] in ("jpg", "png", "gif"):
                type = immp.File.Type.image
            attachments.append(immp.File(title=attach.filename,
                                         type=type,
                                         source=attach.url))
        for embed in message.embeds:
            if embed.image.url and embed.image.url.rsplit(".", 1)[1] in ("jpg", "png", "gif"):
                attachments.append(immp.File(type=immp.File.Type.image,
                                             source=embed.image.url))
        return (immp.Channel(discord, message.channel.id),
                cls(id=message.id,
                    at=message.created_at,
                    text=DiscordRichText.from_markdown(discord, text) if text else None,
                    user=DiscordUser.from_user(discord, message.author),
                    attachments=attachments,
                    raw=message))


class DiscordClient(discord.Client):
    """
    Subclass of the underlying client to bind events.
    """

    def __init__(self, plug, **kwargs):
        super().__init__(**kwargs)
        self._plug = plug

    async def on_ready(self):
        with await self._plug._starting:
            self._plug._starting.notify_all()

    async def on_message(self, message):
        log.debug("Received a new message")
        channel, msg = DiscordMessage.from_message(self._plug, message)
        self._plug.queue(channel, msg)

    async def on_message_edit(self, before, after):
        log.debug("Received an updated message")
        if before.content == after.content:
            # Text content hasn't changed -- maybe just a link unfurl embed added.
            return
        channel, msg = DiscordMessage.from_message(self._plug, after)
        # Edits don't generate a new ID.
        msg.original = msg.id
        self._plug.queue(channel, msg)

    async def on_message_delete(self, message):
        log.debug("Received a deleted message")
        channel, msg = DiscordMessage.from_message(self._plug, message)
        msg.deleted = True
        self._plug.queue(channel, msg)


class DiscordPlug(immp.Plug):
    """
    Plug for a `Discord <https://discordapp.com>`_ server.
    """

    network_name = "Discord"

    @property
    def network_id(self):
        return "discord:{}".format(self._client.user.id) if self._client else None

    def __init__(self, name, config, host):
        super().__init__(name, _Schema.config(config), host)
        # Connection objects that need to be closed on disconnect.
        self._client = self._task = self._session = None
        self._starting = Condition()

    async def start(self):
        await super().start()
        if self.config["webhooks"]:
            self._session = ClientSession()
        log.debug("Starting client")
        self._client = DiscordClient(self)
        self._task = ensure_future(self._client.start(self.config["token"], bot=self.config["bot"]))
        with await self._starting:
            # Block until the client is ready.
            await self._starting.wait()
        if self.config["playing"]:
            await self._client.change_presence(activity=discord.Game(self.config["playing"]))

    async def stop(self):
        await super().stop()
        if self._client:
            log.debug("Closing client")
            await self._client.close()
            self._client = None
        if self._session:
            log.debug("Closing session")
            await self._session.close()
            self._session = None

    async def user_from_id(self, id):
        user = await self._client.get_user_info(id)
        return DiscordUser.from_user(self, user) if user else None

    async def user_from_username(self, username):
        for guild in self._client.guilds:
            member = guild.get_member_named(username)
            if member:
                return DiscordUser.from_user(self, member)
        return None

    async def channel_for_user(self, user):
        if not isinstance(user, DiscordUser):
            return None
        if not isinstance(user.raw, (discord.Member, discord.User)):
            return None
        dm = user.raw.dm_channel or (await user.raw.create_dm())
        return immp.Channel(None, self, dm.id)

    async def channel_title(self, channel):
        dc_channel = self._client.get_channel(channel.source)
        return dc_channel.name if dc_channel else None

    async def channel_is_private(self, channel):
        dc_channel = self._client.get_channel(channel.source)
        return isinstance(dc_channel, discord.DMChannel)

    async def channel_members(self, channel):
        if channel.plug is not self:
            return None
        dc_channel = self._client.get_channel(channel.source)
        if dc_channel:
            return [DiscordUser.from_user(self, member) for member in dc_channel.members]
        else:
            return []

    async def _put_webhook(self, webhook, msg):
        if msg.deleted:
            # TODO
            return []
        name = image = rich = None
        if msg.user:
            name = msg.user.real_name or msg.user.username
            image = msg.user.avatar
        if msg.text:
            if isinstance(msg.text, immp.RichText):
                rich = msg.text.clone()
            else:
                # Unformatted text received, make a basic rich text instance out of it.
                rich = immp.RichText([immp.Segment(msg.text)])
            if msg.action:
                for segment in rich:
                    segment.italic = True
        data = FormData()
        payload = {}
        embeds = []
        if msg.attachments:
            for i, attach in enumerate(msg.attachments):
                if isinstance(attach, immp.File) and attach.type == immp.File.Type.image:
                    img_resp = await attach.get_content(self._session)
                    filename = attach.title or "image_{}.png".format(i)
                    data.add_field("file_{}".format(i), img_resp.content, filename=filename)
                elif isinstance(attach, immp.Location):
                    embeds.append({"title": attach.name or "Location",
                                   "url": attach.google_map_url,
                                   "description": attach.address,
                                   "thumbnail": {"url": attach.google_image_url(80)},
                                   "footer": {"text": "{}, {}".format(attach.latitude,
                                                                      attach.longitude)}})
        if msg.reply_to:
            quote = {"footer": {"text": "\N{SPEECH BALLOON}"},
                     "timestamp": msg.reply_to.at.isoformat()}
            if msg.reply_to.user:
                quote["author"] = {"name": (msg.reply_to.user.real_name or
                                            msg.reply_to.user.username),
                                   "icon_url": msg.reply_to.user.avatar}
            quoted_rich = None
            quoted_action = False
            if msg.reply_to.text:
                if isinstance(msg.reply_to.text, immp.RichText):
                    quoted_rich = msg.reply_to.text.clone()
                else:
                    quoted_rich = immp.RichText([immp.Segment(msg.reply_to.text)])
            elif msg.reply_to.attachments:
                quoted_action = True
                count = len(msg.reply_to.attachments)
                what = "{} files".format(count) if count > 1 else "this file"
                quoted_rich = immp.RichText([immp.Segment("sent {}".format(what))])
            if quoted_rich:
                if quoted_action:
                    for segment in quoted_rich:
                        segment.italic = True
                quote["description"] = DiscordRichText.to_markdown(self, quoted_rich)
            embeds.append(quote)
        # Null values aren't accepted, only add name/image to data if they're set.
        if name:
            payload["username"] = name
        if image:
            payload["avatar_url"] = image
        if rich:
            payload["content"] = DiscordRichText.to_markdown(self, rich.normalise())
        if embeds:
            payload["embeds"] = embeds
        data.add_field("payload_json", json_dumps(payload))
        async with self._session.post("{}?wait=true".format(webhook), data=data) as resp:
            json = await resp.json()
        message = _Schema.webhook(json)
        return [int(message["id"])]

    async def _put_client(self, channel, msg):
        if msg.deleted:
            # TODO
            return []
        embeds = []
        if msg.attachments:
            for i, attach in enumerate(msg.attachments):
                if isinstance(attach, immp.File) and attach.type == immp.File.Type.image:
                    img_resp = await attach.get_content(self._session)
                    filename = attach.title or "image_{}.png".format(i)
                    embeds.append((None, discord.File(img_resp.content, filename), "an image"))
                elif isinstance(attach, immp.Location):
                    embed = discord.Embed()
                    embed.title = attach.name or "Location"
                    embed.url = attach.google_map_url
                    embed.description = attach.address
                    embed.set_thumbnail(url=attach.google_image_url(80))
                    embed.set_footer(text="{}, {}".format(attach.latitude, attach.longitude))
                    embeds.append((embed, None, "a location"))
        requests = []
        if msg.text or msg.reply_to:
            rich = msg.render()
            embed = file = None
            if len(embeds) == 1:
                # Attach the only embed to the message text.
                embed, file, _ = embeds.pop()
            requests.append(channel.send(content=DiscordRichText.to_markdown(self, rich),
                                         embed=embed, file=file))
        for embed, file, desc in embeds:
            # Send any additional embeds in their own separate messages.
            content = None
            if msg.user:
                label = immp.Message(user=msg.user, text="sent {}".format(desc), action=True)
                content = DiscordRichText.to_markdown(self, label.render())
            requests.append(channel.send(content=content, embed=embed, file=file))
        sent = []
        for request in requests:
            sent.append(await request)
        return [resp.id for resp in sent]

    async def put(self, channel, msg):
        webhook = None
        for label, host_channel in self.host.channels.items():
            if channel == host_channel:
                webhook = self.config["webhooks"].get(label)
                break
        dc_channel = self._client.get_channel(channel.source)
        if webhook:
            log.debug("Sending to {} via webhook".format(repr(channel)))
            return await self._put_webhook(webhook, msg)
        elif dc_channel:
            log.debug("Sending to {} via client".format(repr(channel)))
            return await self._put_client(dc_channel, msg)
        else:
            raise DiscordAPIError("No access to channel {}".format(channel.source))
