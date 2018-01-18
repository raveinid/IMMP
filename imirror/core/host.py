from asyncio import wait
import logging

from aiostream import stream

from .error import ConfigError
from .receiver import Receiver
from .transport import Channel, Transport
from .util import resolve_import, Base


log = logging.getLogger(__name__)


class Host(Base):
    """
    Main class responsible for starting, stopping, and interacting with transports.

    Attributes:
        transports ((str, .Transport) dict):
            Collection of all registered transport instances, keyed by name.
        receivers ((str, .Receiver) dict):
            Collection of all registered message receivers, keyed by name.
        running (bool):
            Whether messages from transports are being processed by the host.
    """

    def __init__(self):
        self.transports = {}
        self.channels = {}
        self.receivers = {}
        self.running = False

    def create_transport(self, name, path, config):
        """
        Create a new named transport according to the provided config.

        Args:
            name (str):
                User-provided, unique name of the transport, used for config references.
            path (str):
                Python dotted name of the form ``<module name>.<class name>``, representing the
                selected transport.
            config (dict):
                Reference to the user-provided configuration.

        Returns:
            .Transport:
                Generated transport instance.
        """
        if name in self.transports:
            raise ConfigError("Transport name '{}' already registered".format(name))
        try:
            cls = resolve_import(path)
        except ImportError as e:
            raise ConfigError("Error trying to import transport class '{}'".format(path)) from e
        if not issubclass(cls, Transport):
            raise ConfigError("Transport class '{}' not a valid subclass".format(path))
        log.debug("Creating transport: {} ({})".format(name, path))
        return cls(name, config, self)

    def add_transport(self, transport):
        """
        Register a transport to the host.

        Args:
            transport (.Transport):
                Existing transport instance.
        """
        log.debug("Adding transport: {}".format(transport.name))
        self.transports[transport.name] = transport

    def remove_transport(self, name):
        """
        Unregister an existing transport.

        Args:
            name (str):
                Name of a previously registered transport instance to disconnect and stop tracking.
        """
        try:
            transport = self.transports[name]
        except KeyError:
            raise RuntimeError("Transport '{}' not registered to host".format(name)) from None
        if transport.connected:
            raise RuntimeError("Transport '{}' still connected".format(name))
        del self.transports[name]

    def add_channel(self, name, transport, source):
        """
        Register a new channel.

        Args:
            name (str):
                User-provided, unique name of the transport, used for config references.
            transport (str):
                Name of the transport that provides this channel.
            source (str):
                Transport-specific channel identifier.
        """
        if name in self.channels:
            raise ConfigError("Channel name '{}' already registered".format(name))
        try:
            transport = self.transports[transport]
        except KeyError:
            raise ConfigError("Channel transport '{}' not registered".format(name)) from None
        log.debug("Adding channel: {} ({} -> {})".format(name, transport.name, source))
        self.channels[name] = Channel(name, transport, source)

    def remove_channel(self, name):
        """
        Unregister an existing channel.

        Args:
            name (str):
                Name of a previously registered channel.
        """
        try:
            del self.channels[name]
        except KeyError:
            raise RuntimeError("Channel '{}' not registered to host".format(name)) from None

    def create_receiver(self, name, path, config):
        """
        Create a new named receiver according to the provided config.

        Args:
            name (str):
                User-provided, unique name of the receiver, used for config references.
            path (str):
                Python dotted name of the form ``<module name>.<class name>``, representing the
                selected receiver.
            config (dict):
                Reference to the user-provided configuration.

        Returns:
            .Receiver:
                Generated receiver instance.
        """
        if name in self.receivers:
            raise ConfigError("Receiver name '{}' already registered".format(name))
        try:
            cls = resolve_import(path)
        except ImportError:
            raise ConfigError("Error trying to import receiver class '{}'".format(path))
        if not issubclass(cls, Receiver):
            raise ConfigError("Receiver class '{}' not a valid subclass".format(path))
        log.debug("Adding receiver: {} ({})".format(name, path))
        return cls(name, config, self)

    def add_receiver(self, receiver):
        """
        Register a receiver to the host.

        Args:
            receiver (.Receiver):
                Existing receiver instance.
        """
        self.receivers[receiver.name] = receiver

    def remove_receiver(self, name):
        """
        Unregister an existing receiver.

        Args:
            receiver (.Receiver):
                Name of a previously registered receiver instance to stop using.
        """
        try:
            del self.receivers[name]
        except KeyError:
            raise RuntimeError("Receiver '{}' not registered to host".format(name)) from None

    def resolve_channel(self, transport, source):
        """
        Take a transport and channel name, and resolve it from the configured channels.

        Args:
            transport (.Transport):
                Registered transport instance.
            source (str):
                Transport-specific channel identifier.

        Returns:
            .Channel:
                Generated channel container object.
        """
        for channel in self.channels.values():
            if channel.transport == transport and channel.source == source:
                return channel
        log.debug("Channel transport/source not found: {}/{}".format(transport.name, source))
        return Channel(None, transport, source)

    async def run(self):
        """
        Connect all transports, and distribute messages to receivers.
        """
        if self.transports:
            log.debug("Connecting transports")
            await wait([transport.connect() for transport in self.transports.values()])
        else:
            log.warn("No transports registered")
        if self.receivers:
            log.debug("Starting receivers")
            await wait([receiver.start() for receiver in self.receivers.values()])
        else:
            log.warn("No receivers registered")
        self.running = True
        getters = (transport.receive() for transport in self.transports.values())
        async with stream.merge(*getters).stream() as streamer:
            async for channel, msg in streamer:
                log.debug("Received: {} {}".format(repr(channel), repr(msg)))
                await wait([receiver.process(channel, msg)
                            for receiver in self.receivers.values()])

    async def close(self):
        """
        Disconnect all open transports.
        """
        await wait([receiver.stop() for receiver in self.receivers.values()])
        await wait([transport.disconnect() for transport in self.transports.values()])
