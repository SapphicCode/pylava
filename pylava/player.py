from inspect import isawaitable
from typing import Optional, Callable

from discord import VoiceChannel, Guild


# noinspection PyProtectedMember
class Player:
    __slots__ = [
        'conn', '_guild', '_channel', '_paused', '_playing', '_position', '_volume', '_track_callback', '_connecting'
    ]

    def __init__(self, connection, guild_id: int):
        self.conn = connection
        self._guild = guild_id
        self._connecting = False
        # dynamic variables:
        self._channel = None
        self._paused = False
        self._playing = False
        self._position = None
        self._volume = 100
        self._track_callback = None

    @property
    def channel(self) -> Optional[VoiceChannel]:
        """Returns the channel the player is connected to."""
        if self._channel is None:
            return
        return self.conn.bot.get_channel(self._channel)

    @property
    def guild(self) -> Guild:
        """Returns the player's guild."""
        return self.conn.bot.get_guild(self._guild)

    @property
    def connected(self) -> bool:
        """Returns the player's connected state."""
        return bool(self._channel)

    @property
    def position(self) -> Optional[float]:
        """Returns the player's current position in seconds."""
        return self._position

    @property
    def paused(self) -> bool:
        """Returns the player's paused state."""
        return self._paused

    @property
    def playing(self) -> bool:
        """Returns the player's playing state."""
        if self.paused:
            return False  # can't be playing when paused can we
        return self._playing

    @property
    def stopped(self) -> bool:
        """Returns the player's stopped (neither playing nor paused) state."""
        return not self.playing and not self.paused

    @property
    def volume(self) -> int:
        """Returns the player's volume."""
        return self._volume

    @property
    def track_callback(self) -> Optional[Callable]:
        """Accesses the track callback.

        This is the callable that will be called with the current player's instance as its first argument.
        It may be awaitable. If it is None, it will be ignored.
        """
        return self._track_callback

    @track_callback.setter
    def track_callback(self, c: Optional[Callable]):
        self._track_callback = c

    async def connect(self, channel_id: int):
        """Connects the player to a Discord channel."""
        self._connecting = True
        await self.conn._discord_connect(self._guild, channel_id)
        self._channel = channel_id

    async def disconnect(self):
        """Disconnects the player from Discord."""
        await self.conn._discord_disconnect(self._guild)
        self._channel = None

    async def query(self, *args, **kwargs):
        """Shortcut method for :meth:`Connection.query`."""
        return await self.conn.query(*args, **kwargs)

    async def play(self, track: str, start_time: float=0.0, end_time: float=None):
        """
        Plays a track. If already playing, replaces the current track.

        :param track: A base64 track ID returned by the :meth:`Connection.query` method.
        :param start_time: (optional) How far into the track to start playing (defaults to 0).
        :param end_time: (optional) At what point in the track to stop playing (defaults to track length).
        """
        await self.conn._discord_play(self._guild, track, start_time, end_time)
        self._playing = True

    async def set_pause(self, paused: bool):
        """Sets the pause state."""
        if paused == self._paused:
            return  # state's already set
        await self.conn._discord_pause(self._guild, paused)
        self._paused = paused

    async def set_volume(self, volume: int):
        """
        Sets the player's volume.

        :param volume: An integer between (and including) 0 and 150.
        """
        self._volume = await self.conn._discord_volume(self._guild, volume)

    async def stop(self):
        """Stops the player."""
        await self.conn._discord_stop(self._guild)
        self._playing = False

    async def seek(self, position: float):
        """Seeks to a specific position in a track."""
        await self.conn._discord_seek(self._guild, position)

    async def _process_event(self, data):
        if data['op'] != 'event':
            return

        if data['type'] == 'TrackEndEvent':
            self._playing = False
            self._position = None

            if not self.track_callback:
                return

            out = self.track_callback(self)
            if isawaitable(out):
                await out
