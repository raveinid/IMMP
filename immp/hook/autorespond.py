"""
Basic text request/response handler.

Config:
    channels (str list):
        List of channels to process responses in.
    responses (dict):
        Mapping from match regex to response text.

Commands:
    ar-add <match> <response>:
        Add a new trigger / response pair.
    ar-remove <match>:
        Remove an existing trigger.

This hook will listen for messages in all given channels, for text content that matches any of the
defined regular expressions.  On a match, it will answer with the corresponding response.

Currently, the commands only add/remove responses for the current session -- changes are lost on
exit as the list will be re-read from config at the next startup.
"""

import re

from voluptuous import ALLOW_EXTRA, All, Length, Optional, Schema

import immp
from immp.hook.command import Commandable


class _Schema(object):

    config = Schema({"channels": All([str], Length(min=1)),
                     Optional("responses", default=dict): {str: str}},
                    extra=ALLOW_EXTRA, required=True)


class AutoRespondHook(immp.Hook, Commandable):
    """
    Basic text responses for given trigger words and phrases.
    """

    def __init__(self, name, config, host):
        super().__init__(name, config, host)
        config = _Schema.config(config)
        self.responses = config["responses"]
        self.channels = []
        for channel in config["channels"]:
            try:
                self.channels.append(host.channels[channel])
            except KeyError:
                raise immp.ConfigError("No channel '{}' on host".format(channel)) from None
        self._sent = []

    def commands(self):
        return {"ar-add": self.add,
                "ar-remove": self.remove}

    async def add(self, channel, msg, match, response):
        self.responses[match] = response
        await channel.send(immp.Message(text="\U00002705 Added"))

    async def remove(self, channel, msg, match):
        del self.responses[match]
        await channel.send(immp.Message(text="\U00002705 Removed"))

    async def process(self, channel, msg):
        await super().process(channel, msg)
        # Only process if we recognise the channel.
        if channel not in self.channels:
            return
        # Skip our own response messages.
        if (channel, msg.id) in self._sent:
            return
        text = str(msg.text)
        for match, response in self.responses.items():
            if re.search(match, text, re.I):
                for id in await channel.send(immp.Message(text=response)):
                    self._sent.append((channel, id))
